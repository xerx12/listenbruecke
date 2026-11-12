"""Experimental, token-only Google Keep adapter for existing checklist notes.

Keep's private API fetches the account's whole note tree. Only explicit checklist
IDs are writable; the tree is never persisted. SDK internals below are pinned to
gkeepapi 0.17.1 / gpsoauth 3.0.0 to bound their otherwise unbounded HTTP calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import gkeepapi
import gpsoauth
import requests

from listenbruecke.connectors.homeassistant import render_label, split_label
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, ShoppingList, UnsupportedOperation

_AUTH_ERROR = "Bitte den Google-Keep-Zugriff mit E-Mail und Master-Token neu einrichten."
_FORMAT_ERROR = "Google Keep hat ein unbekanntes Listenformat geliefert."
_CONFIRM_ERROR = "Google Keep hat die Änderung noch nicht bestätigt. Bitte die Liste neu laden."
_TIMEOUT = (10, 30)
_OPERATION_SECONDS = 90

def _quiet_library() -> None:
    # Upstream debug messages may contain remote node payloads. Never forward
    # them, including children created after this connector was constructed.
    for name in ("gkeepapi", "gkeepapi.node", "gpsoauth"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
        logger.disabled = True


class _BoundedAuth(gkeepapi.APIAuth):
    def __init__(self, scopes: str, check_deadline: Callable[[], None]) -> None:
        super().__init__(scopes)
        self.check_deadline = check_deadline

    def refresh(self) -> str:
        """Use gpsoauth's request builders with an explicit request timeout."""
        self.check_deadline()
        data = gpsoauth.build_oauth_data(
            self._email, self._master_token, self._device_id,
            service=self._scopes, app="com.google.android.keep",
            client_sig="38918a453d07199354f8b19af05ec6562ced5788",
        )
        with requests.Session() as session:
            session.trust_env = False
            session.mount(gpsoauth.AUTH_URL, gpsoauth.AuthHTTPAdapter())
            session.headers.update(gpsoauth.build_auth_headers())
            response = session.post(gpsoauth.AUTH_URL, data=data, verify=True, timeout=_TIMEOUT, allow_redirects=False)
            if 300 <= response.status_code < 400 or response.status_code in (401, 403):
                raise gkeepapi.exception.LoginException("Authentication failed")
            response.raise_for_status()
            result = gpsoauth.google.parse_auth_response(response.text)
        token = result.get("Auth")
        if not isinstance(token, str) or not token:
            raise gkeepapi.exception.LoginException("Authentication failed")
        self._auth_token = token
        return token


class _BoundedKeepAPI(gkeepapi.KeepAPI):
    def __init__(self, check_deadline: Callable[[], None]) -> None:
        super().__init__()
        self._session.trust_env = False
        self.check_deadline = check_deadline

    def send(self, **kwargs: Any) -> dict:
        """Never replay mutations; allow one auth refresh for read-only syncs."""
        self.check_deadline()
        kwargs["timeout"] = _TIMEOUT
        kwargs["allow_redirects"] = False
        payload = kwargs.get("json", {})
        readonly = isinstance(payload, dict) and payload.get("nodes") == [] and "userInfo" not in payload
        for attempt in range(2):
            response = self._send(**kwargs)
            if 300 <= response.status_code < 400:
                raise ConnectorError("Google Keep hat eine unerwartete Antwort geliefert. Bitte später erneut versuchen.")
            if response.status_code in (401, 403):
                code, result = response.status_code, {}
            else:
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ConnectorError(_FORMAT_ERROR)
                error = result.get("error")
                code = error.get("code") if isinstance(error, dict) else None
            # Refresh once for a proven read-only sync. A mutation is never
            # replayed, including when authentication fails in the JSON body.
            if code == 401 and readonly and attempt == 0:
                self._auth.refresh()
                self.check_deadline()
                continue
            if code in (401, 403):
                raise gkeepapi.exception.LoginException("Authentication failed")
            break
        if "error" in result:
            raise ConnectorError("Google Keep ist gerade nicht erreichbar. Bitte später erneut versuchen.")
        if (not isinstance(result.get("toVersion"), str) or not result["toVersion"]
                or not isinstance(result.get("truncated"), bool)
                or ("targetVersion" not in payload and "nodes" not in result)
                or ("nodes" in result and not isinstance(result["nodes"], list))):
            raise ConnectorError(_FORMAT_ERROR)
        seen = set()
        for node in result.get("nodes", []):
            if (not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]
                    or node["id"] in seen
                    or ("parentId" in node and (not isinstance(node["parentId"], str)
                                                or node.get("type") not in ("NOTE", "LIST", "LIST_ITEM", "BLOB")))):
                raise ConnectorError(_FORMAT_ERROR)
            seen.add(node["id"])
            if node.get("type") == "LIST_ITEM" and (not isinstance(node.get("text"), str)
                                                       or not isinstance(node.get("checked"), bool)):
                raise ConnectorError(_FORMAT_ERROR)
        return result


def _new_client(email: str, master_token: str, check_deadline: Callable[[], None]) -> gkeepapi.Keep:
    client = gkeepapi.Keep()
    client._keep_api._session.close()
    client._keep_api = _BoundedKeepAPI(check_deadline)
    auth = _BoundedAuth(client.OAUTH_SCOPES, check_deadline)
    try:
        # Stable within this client, unrelated to any physical device or MAC.
        auth.load(email, master_token, uuid.uuid4().hex[:16])
        client.load(auth, sync=True)
        return client
    except BaseException:
        _close_client(client)
        raise


def _close_client(client: gkeepapi.Keep) -> None:
    for api_name in ("_keep_api", "_reminders_api", "_media_api"):
        api = getattr(client, api_name, None)
        if api is not None:
            api._session.close()
    # No final sync: failed writes must never be retried during cleanup.


class GoogleKeepConnector:
    def __init__(self, credentials: dict, save: Callable | None = None) -> None:
        _quiet_library()
        self.credentials = dict(credentials)
        self._client: gkeepapi.Keep | None = None
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._deadline = 0.0

    def _check_deadline(self) -> None:
        if time.monotonic() >= self._deadline:
            raise TimeoutError

    def _discard(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            _close_client(client)

    def _execute(self, operation: Callable[[gkeepapi.Keep], Any]) -> Any:
        self._deadline = time.monotonic() + _OPERATION_SECONDS
        try:
            email, token = self.credentials.get("email"), self.credentials.get("master_token")
            if not isinstance(email, str) or not email.strip() or not isinstance(token, str) or not token.strip():
                raise AuthenticationRequired(_AUTH_ERROR)
            if self._client is None:
                self._client = _new_client(email.strip(), token.strip(), self._check_deadline)
            else:
                # Complete all remote pages before inspecting or mutating a note.
                self._client.sync()
            return operation(self._client)
        except (AuthenticationRequired, gkeepapi.exception.LoginException):
            self._discard()
            raise AuthenticationRequired(_AUTH_ERROR) from None
        except (ConnectorError, UnsupportedOperation):
            self._discard()
            raise
        except (TimeoutError, requests.Timeout):
            self._discard()
            raise ConnectorError("Google Keep antwortet gerade zu langsam. Bitte später erneut versuchen.") from None
        except Exception:
            self._discard()
            raise ConnectorError("Google Keep konnte nicht erreicht werden. Bitte die Verbindung prüfen.") from None

    async def _run(self, operation: Callable[[gkeepapi.Keep], Any]) -> Any:
        await self._lock.acquire()
        if self._closed:
            self._lock.release()
            raise ConnectorError("Die Google-Keep-Verbindung wurde geschlossen.")
        worker = asyncio.create_task(asyncio.to_thread(self._execute, operation))
        self._worker = worker

        def finished(task: asyncio.Task) -> None:
            self._worker = None
            self._lock.release()
            if not task.cancelled():
                task.exception()  # Consume failures even if the caller was cancelled.

        worker.add_done_callback(finished)
        # Cancellation stops the caller, not its thread. Only the worker's done
        # callback releases the lock, so another operation cannot overlap it.
        return await asyncio.shield(worker)

    @staticmethod
    def _lists(client: gkeepapi.Keep) -> list[gkeepapi.node.List]:
        notes = client.all()
        if not isinstance(notes, list):
            raise ConnectorError(_FORMAT_ERROR)
        result, seen = [], set()
        for note in notes:
            if not isinstance(note, gkeepapi.node.List) or note.deleted or note.trashed or note.archived:
                continue
            if not isinstance(note.id, str) or not note.id or note.id in seen or not isinstance(note.title, str):
                raise ConnectorError(_FORMAT_ERROR)
            seen.add(note.id)
            result.append(note)
        return result

    @classmethod
    def _selected(cls, client: gkeepapi.Keep, list_id: str) -> gkeepapi.node.List:
        note = next((note for note in cls._lists(client) if note.id == list_id), None)
        if note is None:
            raise ConnectorError("Die ausgewählte Google-Keep-Liste ist nicht mehr verfügbar. Bitte neu auswählen.")
        return note

    @staticmethod
    def _items(note: gkeepapi.node.List) -> list[Item]:
        result, seen = [], set()
        for node in note.items:
            if (not isinstance(node, gkeepapi.node.ListItem) or not isinstance(node.id, str)
                    or not node.id or node.id in seen or node.parent_id != note.id
                    or not isinstance(node.text, str) or not isinstance(node.checked, bool)):
                raise ConnectorError(_FORMAT_ERROR)
            seen.add(node.id)
            name, description = split_label(node.text)
            has_children = any(not child.deleted for child in node.subitems)
            result.append(Item(node.id, name, description, node.checked, deletable=not has_children,
                               metadata={"note_id": note.id, "indented": node.indented}))
        return result

    @classmethod
    def _current(cls, client: gkeepapi.Keep, list_id: str, item: Item) -> tuple[gkeepapi.node.List, gkeepapi.node.ListItem, Item]:
        if item.metadata.get("note_id", list_id) != list_id:
            raise ConnectorError("Der Google-Keep-Artikel gehört zu einer anderen Liste.")
        note = cls._selected(client, list_id)
        current = next((row for row in cls._items(note) if row.uid == item.uid), None)
        if current is None:
            raise ConnectorError("Der Google-Keep-Artikel ist nicht mehr verfügbar. Bitte die Liste neu laden.")
        node = next(node for node in note.items if node.id == current.uid)
        return note, node, current

    @staticmethod
    def _validate_fields(name: str, description: str, completed: bool) -> None:
        if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not isinstance(completed, bool):
            raise ConnectorError("Bitte einen Artikelnamen und gültige Artikeldaten eingeben.")

    @classmethod
    def _confirm(cls, client: gkeepapi.Keep, list_id: str, uid: str, name: str, description: str, completed: bool) -> Item:
        # save() clears SDK dirty flags before HTTP acknowledgement. A full fresh
        # read is essential: inspecting that local tree would falsely confirm a
        # rejected update. resync=True clears every local change before its read.
        client.sync()
        client.sync(resync=True)
        note = cls._selected(client, list_id)
        node = next((node for node in note.items if node.id == uid), None)
        if node is None or node.dirty or not node.server_id:
            raise ConnectorError(_CONFIRM_ERROR)
        current = next((row for row in cls._items(note) if row.uid == uid), None)
        if current is None or current.fields() != {"name": name, "description": description, "completed": completed}:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def test_connection(self) -> list[ShoppingList]:
        return await self.list_lists()

    async def list_lists(self) -> list[ShoppingList]:
        return await self._run(lambda client: [ShoppingList(note.id, note.title or "Liste ohne Titel") for note in self._lists(client)])

    async def get_items(self, list_id: str) -> list[Item]:
        return await self._run(lambda client: self._items(self._selected(client, list_id)))

    async def add_item(self, list_id: str, name: str, description: str = "", completed: bool = False) -> Item:
        self._validate_fields(name, description, completed)

        def add(client: gkeepapi.Keep) -> Item:
            note = self._selected(client, list_id)
            before = {row.uid for row in self._items(note)}
            node = note.add(render_label(name, description), completed)
            if node.id in before:
                raise ConnectorError(_FORMAT_ERROR)
            return self._confirm(client, list_id, node.id, name, description, completed)

        return await self._run(add)

    async def update_item(self, list_id: str, item: Item, *, name: str | None = None,
                          description: str | None = None, completed: bool | None = None) -> Item:
        def update(client: gkeepapi.Keep) -> Item:
            _, node, current = self._current(client, list_id, item)
            for field, requested in (("name", name), ("description", description), ("completed", completed)):
                if requested is not None and getattr(current, field) not in (getattr(item, field), requested):
                    raise ConnectorError("Der Google-Keep-Artikel wurde inzwischen geändert. Bitte die Liste neu laden.")
            new_name = current.name if name is None else name
            new_description = current.description if description is None else description
            new_completed = current.completed if completed is None else completed
            self._validate_fields(new_name, new_description, new_completed)
            if (new_name, new_description) != (current.name, current.description):
                node.text = render_label(new_name, new_description)
            if new_completed != current.completed:
                node.checked = new_completed
            return self._confirm(client, list_id, node.id, new_name, new_description, new_completed)

        return await self._run(update)

    async def delete_item(self, list_id: str, item: Item) -> None:
        def delete(client: gkeepapi.Keep) -> None:
            note, node, current = self._current(client, list_id, item)
            if current.fields() != item.fields():
                raise ConnectorError("Der Google-Keep-Artikel wurde inzwischen geändert. Bitte die Liste neu laden.")
            if not current.deletable:
                raise UnsupportedOperation("Google-Keep-Artikel mit Unterpunkten können nicht automatisch gelöscht werden.")
            uid = node.id
            node.delete()
            client.sync()
            client.sync(resync=True)
            note = self._selected(client, list_id)
            # A locally marked deletion alone is no server confirmation. Keep's
            # acknowledgement removes the child from the parent's children list.
            if any(child.id == uid for child in note.children):
                raise ConnectorError(_CONFIRM_ERROR)

        await self._run(delete)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._discard)
