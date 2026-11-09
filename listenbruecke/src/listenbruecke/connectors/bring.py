"""Bring adapter using the current typed bring-api client and UUID operations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import aiohttp
from bring_api import Bring
from bring_api.exceptions import BringAuthException, BringException, BringMissingFieldException
from bring_api.types import BringItemOperation

from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, ShoppingList

_FORMAT_ERROR = "Bring! hat ein unbekanntes Listenformat geliefert."
_CONFIRM_ERROR = "Bring! hat die Änderung noch nicht bestätigt. Bitte die Liste neu laden."


class BringConnector:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self.credentials = credentials
        self._session: aiohttp.ClientSession | None = None
        self._api: Bring | None = None
        self._login_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # The upstream library logs response bodies at DEBUG. Account data and
        # authentication error bodies must not appear in application logs.
        logging.getLogger("bring_api").setLevel(logging.WARNING)

    async def _ensure_login(self) -> Bring:
        async with self._login_lock:
            if self._api is not None:
                return self._api
            email = self.credentials.get("email")
            password = self.credentials.get("password")
            if not isinstance(email, str) or not email.strip() or not isinstance(password, str) or not password:
                raise AuthenticationRequired("Bitte E-Mail und Passwort für Bring! eingeben.")
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            api = Bring(self._session, email, password)
            await self._call(api.login)
            self._api = api
            return api

    @staticmethod
    async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BringAuthException:
            raise AuthenticationRequired("Die Bring!-Anmeldung ist ungültig. Bitte das Konto neu verbinden.") from None
        except (BringException, BringMissingFieldException, aiohttp.ClientError, TimeoutError):
            raise ConnectorError("Bring! ist gerade nicht erreichbar oder hat ungültige Daten geliefert.") from None

    async def test_connection(self) -> list[ShoppingList]:
        return await self.list_lists()

    async def list_lists(self) -> list[ShoppingList]:
        api = await self._ensure_login()
        response = await self._call(api.load_lists)
        try:
            rows = response.lists
            if not isinstance(rows, list):
                raise ValueError
            result = []
            for row in rows:
                if not isinstance(row.listUuid, str) or not row.listUuid or not isinstance(row.name, str):
                    raise ValueError
                result.append(ShoppingList(row.listUuid, row.name))
            if len({r.id for r in result}) != len(result):
                raise ValueError
            return result
        except (AttributeError, TypeError, ValueError):
            raise ConnectorError(_FORMAT_ERROR) from None

    async def _list(self, list_id: str) -> None:
        if not any(row.id == list_id for row in await self.list_lists()):
            raise ConnectorError("Die ausgewählte Bring!-Liste ist nicht mehr verfügbar.")

    async def get_items(self, list_id: str) -> list[Item]:
        await self._list(list_id)
        assert self._api is not None
        response = await self._call(self._api.get_list, list_id)
        try:
            if response.uuid != list_id:
                raise ValueError
            purchase, recently = response.items.purchase, response.items.recently
            if not isinstance(purchase, list) or not isinstance(recently, list):
                raise ValueError
            result = []
            for completed, rows in ((False, purchase), (True, recently)):
                for row in rows:
                    if (not isinstance(row.uuid, str) or not row.uuid or
                            not isinstance(row.itemId, str) or not row.itemId.strip() or
                            not isinstance(row.specification, str)):
                        raise ValueError
                    result.append(Item(row.uuid, row.itemId, row.specification, completed))
            if len({i.uid for i in result}) != len(result):
                raise ValueError
            return result
        except (AttributeError, TypeError, ValueError):
            raise ConnectorError(_FORMAT_ERROR) from None

    async def _current(self, list_id: str, item: Item) -> Item:
        current = next((i for i in await self.get_items(list_id) if i.uid == item.uid), None)
        if current is None:
            raise ConnectorError("Der Bring!-Eintrag wurde inzwischen entfernt.")
        return current

    async def _confirm(self, list_id: str, uid: str, name: str, description: str, completed: bool) -> Item:
        current = next((i for i in await self.get_items(list_id) if i.uid == uid), None)
        if current is None or current.fields() != {
            "name": name, "description": description, "completed": completed,
        }:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def add_item(self, list_id: str, name: str, description: str = "", completed: bool = False) -> Item:
        if not name.strip():
            raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
        async with self._write_lock:
            await self._list(list_id)
            assert self._api is not None
            uid = str(uuid4())
            payload = {"itemId": name, "spec": description, "uuid": uid}
            await self._call(self._api.batch_update_list, list_id, payload, BringItemOperation.ADD)
            if completed:
                await self._call(self._api.batch_update_list, list_id, payload, BringItemOperation.COMPLETE)
            return await self._confirm(list_id, uid, name, description, completed)

    async def update_item(self, list_id: str, item: Item, *, name: str | None = None,
                          description: str | None = None, completed: bool | None = None) -> Item:
        async with self._write_lock:
            current = await self._current(list_id, item)
            assert self._api is not None
            new_name = current.name if name is None else name
            new_description = current.description if description is None else description
            new_completed = current.completed if completed is None else completed
            if not new_name.strip():
                raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
            if (new_name, new_description, new_completed) == (current.name, current.description, current.completed):
                return current
            if new_name != current.name:
                # Bring caches names per UUID. A new UUID avoids showing the old
                # name in its app. Explicit UUIDs preserve same-name duplicates.
                uid = str(uuid4())
                replacement = {"itemId": new_name, "spec": new_description, "uuid": uid}
                await self._call(self._api.batch_update_list, list_id, replacement, BringItemOperation.ADD)
                if new_completed:
                    await self._call(self._api.batch_update_list, list_id, replacement, BringItemOperation.COMPLETE)
                # Keep the original until its replacement has been read back.
                await self._confirm(list_id, uid, new_name, new_description, new_completed)
                await self._call(self._api.batch_update_list, list_id,
                                 {"itemId": current.name, "spec": current.description, "uuid": current.uid},
                                 BringItemOperation.REMOVE)
                rows = await self.get_items(list_id)
                if any(i.uid == current.uid for i in rows):
                    raise ConnectorError(_CONFIRM_ERROR)
            else:
                uid = current.uid
                payload = {"itemId": current.name, "spec": new_description, "uuid": uid}
                await self._call(self._api.batch_update_list, list_id, payload,
                                 BringItemOperation.COMPLETE if new_completed else BringItemOperation.ADD)
            return await self._confirm(list_id, uid, new_name, new_description, new_completed)

    async def delete_item(self, list_id: str, item: Item) -> None:
        async with self._write_lock:
            current = await self._current(list_id, item)
            assert self._api is not None
            await self._call(self._api.batch_update_list, list_id,
                             {"itemId": current.name, "spec": current.description, "uuid": current.uid},
                             BringItemOperation.REMOVE)
            if any(i.uid == current.uid for i in await self.get_items(list_id)):
                raise ConnectorError(_CONFIRM_ERROR)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._api = None
