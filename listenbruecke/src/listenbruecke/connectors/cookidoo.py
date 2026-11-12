"""Cookidoo shopping-list adapter using the pinned cookidoo-api 0.19.0 SDK.

Recipe ingredients expose checkbox changes only. Arbitrary incoming items are
added to Cookidoo's additional purchases, with reversible visible quantities.
OAuth tokens are saved through the caller's encrypted store; no login files.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
import hashlib
import json
import logging
import math
from typing import Any

import aiohttp
from cookidoo_api import (Cookidoo, CookidooAdditionalItem, CookidooAuthData,
                         CookidooAuthException, CookidooConfig, CookidooException,
                         CookidooIngredientItem, get_localization_options)

from listenbruecke.connectors.homeassistant import render_label, split_label
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, ShoppingList, UnsupportedOperation

_FORMAT_ERROR = "Cookidoo hat ein unbekanntes oder unvollständiges Listenformat geliefert."
_AUTH_ERROR = "Die Cookidoo-Anmeldung ist ungültig oder benötigt eine Bestätigung. Bitte E-Mail, Passwort und Kontoregion prüfen und erneut verbinden."
_CONFIRM_ERROR = "Cookidoo hat die Änderung noch nicht eindeutig bestätigt. Bitte die Liste erneut laden."


def _quiet_library():
    # The upstream SDK logs complete response bodies at DEBUG. Even provider
    # exceptions must never send cookies, OAuth payloads or credentials to logs.
    logger = logging.getLogger("cookidoo_api")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)


def _auth_data(value):
    if (not isinstance(value, dict) or set(value) != {"access_token", "refresh_token", "expires_at"}
            or any(not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 32768
                   for key in ("access_token", "refresh_token"))
            or type(value.get("expires_at")) not in (int, float)
            or not math.isfinite(value["expires_at"]) or value["expires_at"] < 0):
        raise AuthenticationRequired(_AUTH_ERROR)
    return copy.deepcopy(value)


class _DurableCookidoo(Cookidoo):
    """Flush transparent refreshes before any shopping-list request is sent.

    The pinned SDK callback is synchronous and swallows callback failures.
    Buffering it and awaiting our own persistence here prevents a mutation from
    following a failed encrypted-store update. No vendor source is copied.
    """

    def __init__(self, *args, persist, **kwargs):
        super().__init__(*args, **kwargs)
        self._persist_tokens = persist

    async def _request_json(self, method, url, operation, **kwargs):
        await self._ensure_token()
        await self._persist_tokens()
        result = await super()._request_json(method, url, operation, **kwargs)
        if operation in ("loading ingredient items", "loading additional items"):
            if (not isinstance(result, dict)
                    or any(not isinstance(result.get(key), list) for key in ("recipes", "customerRecipes", "additionalItems"))
                    or result.get("next") or result.get("nextToken") or result.get("hasMore")
                    or isinstance(result.get("_links"), dict) and result["_links"].get("next")):
                # This endpoint returns the whole list, with no pagination in
                # 0.19.0. Reject a future paged format rather than truncate it.
                raise ConnectorError(_FORMAT_ERROR)
        return result


class CookidooConnector:
    def __init__(self, credentials, save_credentials):
        self.credentials = {key: copy.deepcopy(credentials[key]) for key in
                            ("email", "password", "country", "language", "auth_data") if key in credentials}
        self.credentials.setdefault("country", "de")
        self.credentials.setdefault("language", "de-DE")
        if isinstance(self.credentials["country"], str):
            self.credentials["country"] = self.credentials["country"].lower()
        self._save_credentials = save_credentials
        self._saved = self._fingerprint()
        self._session = self._api = None
        self._operation_lock = asyncio.Lock()
        self._pending_create = None
        _quiet_library()

    def _fingerprint(self):
        try:
            return hashlib.sha256(json.dumps(self.credentials, sort_keys=True).encode()).digest()
        except (TypeError, ValueError):
            raise AuthenticationRequired(_AUTH_ERROR) from None

    def _capture_auth(self, value):
        self.credentials["auth_data"] = _auth_data(asdict(value))
        self.credentials.pop("password", None)

    async def _persist(self):
        if self._api is not None and self._api.auth_data is not None:
            self._capture_auth(self._api.auth_data)
        current = self._fingerprint()
        if current != self._saved:
            try:
                await self._save_credentials(copy.deepcopy(self.credentials))
            except Exception:
                raise ConnectorError("Die erneuerte Cookidoo-Anmeldung konnte nicht gespeichert werden.") from None
            self._saved = current

    async def _call(self, function, *args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except CookidooAuthException:
            raise AuthenticationRequired(_AUTH_ERROR) from None
        except (CookidooException, aiohttp.ClientError, TimeoutError):
            raise ConnectorError("Cookidoo ist derzeit nicht erreichbar oder hat die Anfrage abgelehnt.") from None
        except ConnectorError:
            raise
        except Exception:
            raise ConnectorError(_FORMAT_ERROR) from None
        finally:
            # The SDK can rotate tokens before a later response or parse error.
            await self._persist()

    async def _ensure_api(self):
        if self._api is not None:
            await self._persist()
            return self._api
        email = self.credentials.get("email")
        country, language = self.credentials["country"], self.credentials["language"]
        if (not isinstance(email, str) or "@" not in email or len(email) > 320
                or not isinstance(country, str) or not isinstance(language, str)):
            raise AuthenticationRequired(_AUTH_ERROR)
        options = await self._call(get_localization_options, country=country, language=language)
        if len(options) != 1:
            raise ConnectorError("Diese Cookidoo-Kombination aus Kontoregion und Sprache wird nicht unterstützt.")
        data = self.credentials.get("auth_data")
        password = self.credentials.get("password", "")
        if data is not None:
            data = _auth_data(data)
        elif not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise AuthenticationRequired(_AUTH_ERROR)
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=30), trust_env=False)
        try:
            cfg = CookidooConfig(localization=options[0], email=email.strip(), password=password)
            api = _DurableCookidoo(self._session,
                cfg=cfg,
                on_auth_data_update=self._capture_auth, persist=self._persist)
            self._api = api
            if data is not None:
                api.apply_auth_data(CookidooAuthData(**data))
                self.credentials.pop("password", None)
                await self._persist()
            else:
                await self._call(api.login)
                if api.auth_data is None:
                    raise AuthenticationRequired(_AUTH_ERROR)
            cfg.password = ""
            return api
        except BaseException as exc:
            self._api = None
            await self._session.close()
            self._session = None
            if isinstance(exc, (ConnectorError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise ConnectorError(_FORMAT_ERROR) from None

    @staticmethod
    def _list(list_id):
        if list_id != "shopping":
            raise ConnectorError("Diese Cookidoo-Einkaufsliste ist nicht bekannt.")

    async def list_lists(self):
        return [ShoppingList("shopping", "Cookidoo-Einkaufsliste")]

    async def test_connection(self):
        await self.get_items("shopping")
        return await self.list_lists()

    async def _items(self):
        api = await self._ensure_api()
        result = []
        for kind, function in (("ingredient", api.get_ingredient_items), ("additional", api.get_additional_items)):
            rows = await self._call(function)
            if not isinstance(rows, list):
                raise ConnectorError(_FORMAT_ERROR)
            for row in rows:
                expected_type = CookidooIngredientItem if kind == "ingredient" else CookidooAdditionalItem
                if (not isinstance(row, expected_type) or not isinstance(row.id, str) or not row.id or len(row.id) > 2048
                        or not isinstance(row.name, str) or not row.name.strip()
                        or type(row.is_owned) is not bool):
                    raise ConnectorError(_FORMAT_ERROR)
                if kind == "ingredient":
                    if not isinstance(row.description, str):
                        raise ConnectorError(_FORMAT_ERROR)
                    name, description = row.name, row.description
                else:
                    name, description = split_label(row.name)
                if not name.strip():
                    raise ConnectorError(_FORMAT_ERROR)
                result.append(Item(("i:" if kind == "ingredient" else "a:") + row.id,
                    name, description, row.is_owned, editable=kind == "additional", deletable=kind == "additional",
                    metadata={"kind": kind, "raw_id": row.id, "provider_label": row.name}))
        if len({item.uid for item in result}) != len(result):
            raise ConnectorError(_FORMAT_ERROR)
        if self._pending_create:
            pending = self._pending_create
            if len([item for item in result if item.uid not in pending["before"]
                    and item.fields() == pending["fields"] and item.metadata["kind"] == "additional"]) == 1:
                self._pending_create = None  # The durable sync engine can adopt it on this read.
        return result

    async def get_items(self, list_id):
        self._list(list_id)
        async with self._operation_lock:
            return await self._items()

    @staticmethod
    def _fields(name, description, completed):
        if (not isinstance(name, str) or not name.strip() or not isinstance(description, str)
                or type(completed) is not bool or len(render_label(name, description)) > 4096):
            raise ConnectorError("Bitte einen gültigen Einkaufsartikel mit Namen und Mengenangabe eingeben.")
        return {"name": name, "description": description, "completed": completed}

    async def _current(self, item):
        current = next((row for row in await self._items() if row.uid == item.uid), None)
        if current is not None and current.fields() != item.fields():
            raise ConnectorError("Der Cookidoo-Artikel wurde inzwischen geändert. Bitte die Liste erneut laden.")
        return current

    async def _confirm(self, uid, fields):
        current = next((row for row in await self._items() if row.uid == uid), None)
        if current is None or current.fields() != fields:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def _ownership(self, item, completed):
        api = await self._ensure_api()
        if item.metadata["kind"] == "ingredient":
            row = CookidooIngredientItem(id=item.metadata["raw_id"], name=item.name,
                                        description=item.description, is_owned=completed)
            await self._call(api.edit_ingredient_items_ownership, [row])
        else:
            row = CookidooAdditionalItem(id=item.metadata["raw_id"], name=item.metadata["provider_label"], is_owned=completed)
            await self._call(api.edit_additional_items_ownership, [row])

    async def _finish_create(self, pending):
        fields = pending["fields"]
        candidates = [row for row in await self._items() if row.uid not in pending["before"]
                      and row.metadata["kind"] == "additional"
                      and (row.name, row.description) == (fields["name"], fields["description"])]
        if len(candidates) != 1:
            raise ConnectorError(_CONFIRM_ERROR)
        current = candidates[0]
        if pending.get("uid") is not None and pending["uid"] != current.uid:
            raise ConnectorError(_CONFIRM_ERROR)
        if current.completed != fields["completed"]:
            await self._ownership(current, fields["completed"])
        confirmed = await self._confirm(current.uid, fields)
        self._pending_create = None
        return confirmed

    async def add_item(self, list_id, name, description="", completed=False):
        self._list(list_id)
        fields = self._fields(name, description, completed)
        async with self._operation_lock:
            if self._pending_create:
                if self._pending_create["fields"] != fields:
                    raise ConnectorError(_CONFIRM_ERROR)
                return await self._finish_create(self._pending_create)
            before = {row.uid for row in await self._items()}
            pending = {"before": before, "fields": fields}
            self._pending_create = pending
            api = await self._ensure_api()
            created = await self._call(api.add_additional_items, [render_label(name, description)])
            if (not isinstance(created, list) or len(created) != 1
                    or not isinstance(created[0].id, str) or not created[0].id):
                raise ConnectorError(_CONFIRM_ERROR)
            pending["uid"] = "a:" + created[0].id
            return await self._finish_create(pending)

    async def update_item(self, list_id, item, *, name=None, description=None, completed=None):
        self._list(list_id)
        async with self._operation_lock:
            current = await self._current(item)
            if current is None:
                raise ConnectorError("Der Cookidoo-Artikel wurde inzwischen entfernt.")
            fields = self._fields(current.name if name is None else name,
                                  current.description if description is None else description,
                                  current.completed if completed is None else completed)
            changed_label = (current.name, current.description) != (fields["name"], fields["description"])
            if changed_label and not current.editable:
                raise UnsupportedOperation("Bei Cookidoo-Rezeptzutaten kann nur der Haken geändert werden.")
            if current.completed != fields["completed"]:
                await self._ownership(current, fields["completed"])
                current = await self._confirm(current.uid, {**current.fields(), "completed": fields["completed"]})
            if changed_label:
                api = await self._ensure_api()
                await self._call(api.edit_additional_items, [CookidooAdditionalItem(
                    id=current.metadata["raw_id"], name=render_label(fields["name"], fields["description"]),
                    is_owned=current.completed)])
            return await self._confirm(current.uid, fields)

    async def delete_item(self, list_id, item):
        self._list(list_id)
        async with self._operation_lock:
            current = await self._current(item)
            if current is None:
                return
            if not current.deletable:
                raise UnsupportedOperation("Einzelne Cookidoo-Rezeptzutaten können nicht gelöscht werden.")
            api = await self._ensure_api()
            await self._call(api.remove_additional_items, [current.metadata["raw_id"]])
            if any(row.uid == current.uid for row in await self._items()):
                raise ConnectorError(_CONFIRM_ERROR)

    async def close(self):
        if self._session is not None:
            await self._session.close()
        self._session = self._api = None
