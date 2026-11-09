"""Direct Alexa lists using aioamazondevices 17.2.8 authentication.

The narrowly scoped wire adapter preserves original labels and follows every
page, avoiding that release's capitalization and first-100-items limitations.
No Amazon password, OTP, debug response or login file is persisted here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.exceptions import AmazonError, CannotAuthenticate, WrongMethod
from aioamazondevices.structures import AmazonSaveDataConfig
from yarl import URL

from listenbruecke.connectors.homeassistant import render_label, split_label
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, ShoppingList

_FORMAT_ERROR = "Alexa hat ein unbekanntes oder unvollständiges Listenformat geliefert."
_CONFIRM_ERROR = "Alexa hat die Änderung noch nicht eindeutig bestätigt. Bitte die Liste prüfen und erneut laden."
_AUTH_ERROR = "Die Amazon-Anmeldung ist abgelaufen oder benötigt eine Bestätigung. Bitte Amazon mit einem aktuellen Authenticator-Code neu verbinden."
_DOMAINS = {"com", "ca", "com.mx", "com.br", "co.uk", "de", "fr", "it", "es", "nl",
            "co.jp", "in", "com.au", "ae", "sa", "sg", "se", "pl", "com.be", "ie", "com.tr", "eg", "co.za"}


def _quiet_library():
    # Vendor debug logging includes request URLs, cookies and response content.
    logger = logging.getLogger("aioamazondevices")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)


def _new_api(session, email, password, login_data=None):
    _quiet_library()
    # callback=None disables raw HTTP debug output. The list-only calls never
    # use the library's unrelated device-settings file at this path.
    return AmazonEchoApi(session, email, password,
                         save_data=AmazonSaveDataConfig(path=Path("."), callback=None),
                         login_data=login_data)


async def _call(function, *args, **kwargs):
    try:
        return await function(*args, **kwargs)
    except (CannotAuthenticate, WrongMethod):
        raise AuthenticationRequired(_AUTH_ERROR) from None
    except (AmazonError, aiohttp.ClientError, TimeoutError):
        raise ConnectorError("Amazon Alexa ist derzeit nicht erreichbar oder hat die Anfrage abgelehnt.") from None
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError(_FORMAT_ERROR) from None


def _check_session(data):
    if (not isinstance(data, dict) or not isinstance(data.get("refresh_token"), str)
            or not data["refresh_token"] or not isinstance(data.get("device_info"), dict)
            or not isinstance(data.get("website_cookies"), dict)
            or not isinstance(data.get("store_authentication_cookie"), dict)):
        raise AuthenticationRequired(_AUTH_ERROR)
    try:
        json.dumps(data)
    except (TypeError, ValueError):
        raise AuthenticationRequired(_AUTH_ERROR) from None
    return data


class AlexaConnector:
    def __init__(self, credentials: dict[str, Any],
                 save_credentials: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.credentials = {"email": credentials.get("email"),
                            "login_data": copy.deepcopy(credentials.get("login_data"))}
        self._save_credentials = save_credentials
        self._saved = self._fingerprint()
        self._session = None
        self._api = None
        self._login_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._lists = {}
        self._lists_at = 0.0
        self._pending_creates = {}
        _quiet_library()

    @classmethod
    async def login(cls, email: str, password: str, code: str) -> dict[str, Any]:
        if (not isinstance(email, str) or "@" not in email or len(email) > 320
                or not isinstance(password, str) or not 1 <= len(password) <= 1024
                or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,8}", code.strip())):
            raise AuthenticationRequired("Bitte Amazon-E-Mail, Passwort und den aktuellen Authenticator-Code eingeben.")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45), trust_env=False) as session:
            try:
                api = _new_api(session, email.strip(), password)
            except Exception:
                raise ConnectorError("Die Amazon-Anmeldung konnte nicht vorbereitet werden.") from None
            data = _check_session(await _call(api.login.login_mode_interactive, code.strip()))
            return {"email": email.strip(), "login_data": copy.deepcopy(data)}

    def _fingerprint(self):
        try:
            return hashlib.sha256(json.dumps(self.credentials, sort_keys=True).encode()).digest()
        except (TypeError, ValueError):
            return None

    async def _persist(self):
        fingerprint = self._fingerprint()
        if fingerprint is None:
            raise AuthenticationRequired(_AUTH_ERROR)
        if fingerprint != self._saved:
            try:
                await self._save_credentials(copy.deepcopy(self.credentials))
            except Exception:
                raise ConnectorError("Die erneuerte Amazon-Anmeldung konnte nicht gespeichert werden.") from None
            self._saved = fingerprint

    async def _ensure_login(self):
        async with self._login_lock:
            if self._api is not None:
                return self._api
            email = self.credentials.get("email")
            data = _check_session(self.credentials.get("login_data"))
            if not isinstance(email, str) or not email.strip():
                raise AuthenticationRequired(_AUTH_ERROR)
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=False)
            try:
                api = _new_api(self._session, email, "", data)
            except Exception:
                raise AuthenticationRequired(_AUTH_ERROR) from None
            updated = await _call(api.login.login_mode_stored_data)
            self.credentials["login_data"] = _check_session(updated)
            await self._persist()
            self._api = api
            return api

    async def _renew(self, api):
        async with self._login_lock:
            self.credentials["login_data"] = _check_session(await _call(api.login.login_mode_stored_data))
            # This pinned release exposes no public retail-cookie refresh method.
            # Renew those cookies through its own login implementation rather
            # than retrying forever with an expired website session.
            await _call(api.login._refresh_auth_cookies)
            await self._persist()

    async def _wire(self, method, parts, *, body=None, query=None, read=True):
        api = await self._ensure_login()
        if not isinstance(api.domain, str) or api.domain not in _DOMAINS:
            raise ConnectorError("Diese Amazon-Region wird noch nicht unterstützt.")
        url = URL("https://www.amazon." + api.domain + "/alexashoppinglists/api/v2/lists/"
                  + "/".join(quote(part, safe="") for part in parts), encoded=True)
        if query:
            url = url.with_query(query)
        async def execute():
            # In particular, never let the vendor retry an ambiguous item POST
            # on 500/503/429 and accidentally create duplicate shopping entries.
            _, response = await _call(api._http_wrapper.session_request, method=method, url=url,
                                     input_data=body or {}, json_data=True, fail_fast=True)
            if response.status != 200:
                raise ConnectorError(_FORMAT_ERROR)
            if not read:
                return None
            result = await _call(response.json, content_type=None)
            if not isinstance(result, dict):
                raise ConnectorError(_FORMAT_ERROR)
            return result
        try:
            try:
                return await execute()
            except AuthenticationRequired:
                await self._renew(api)
                return await execute()
        finally:
            # Preserve refresh rotations even when a subsequent API call fails.
            await self._persist()

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 32 for c in value):
            raise ConnectorError(_FORMAT_ERROR)
        return value

    async def test_connection(self):
        return await self.list_lists()

    async def list_lists(self) -> list[ShoppingList]:
        response = await self._wire("POST", ("fetch",))
        rows = response.get("listInfoList")
        if not isinstance(rows, list):
            raise ConnectorError(_FORMAT_ERROR)
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get("listType") not in ("SHOP", "TODO", "CUSTOM"):
                raise ConnectorError(_FORMAT_ERROR)
            uid = self._identifier(row.get("listId"))
            name = row.get("listName")
            if name is not None and not isinstance(name, str):
                raise ConnectorError(_FORMAT_ERROR)
            name = name or {"SHOP": "Alexa-Einkaufsliste", "TODO": "Alexa-Aufgabenliste", "CUSTOM": "Alexa-Liste"}[row["listType"]]
            result.append(ShoppingList(uid, name))
        if len({row.id for row in result}) != len(result):
            raise ConnectorError(_FORMAT_ERROR)
        self._lists = {row.id: row for row in result}
        self._lists_at = time.monotonic()
        return result

    async def _list(self, list_id):
        self._identifier(list_id)
        if not self._lists_at or time.monotonic() - self._lists_at > 300:
            await self.list_lists()
        if list_id not in self._lists:
            raise ConnectorError("Die ausgewählte Alexa-Liste ist nicht mehr verfügbar.")

    async def get_items(self, list_id: str) -> list[Item]:
        await self._list(list_id)
        items, tokens, next_token = {}, set(), None
        for _ in range(100):
            response = await self._wire("POST", (list_id, "items", "fetch"),
                                        query={"limit": 100}, body={"nextToken": next_token} if next_token else {})
            rows = response.get("itemInfoList")
            if not isinstance(rows, list) or (len(rows) >= 100 and "nextToken" not in response):
                raise ConnectorError(_FORMAT_ERROR)
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("itemName"), str)
                        or not row["itemName"].strip() or row.get("itemStatus") not in ("ACTIVE", "COMPLETE")
                        or type(row.get("version")) is not int or row["version"] < 0):
                    raise ConnectorError(_FORMAT_ERROR)
                uid = self._identifier(row.get("itemId"))
                if uid in items:
                    raise ConnectorError(_FORMAT_ERROR)
                name, description = split_label(row["itemName"])
                items[uid] = Item(uid, name, description, row["itemStatus"] == "COMPLETE",
                                  metadata={"version": row["version"], "provider_label": row["itemName"]})
            next_token = response.get("nextToken")
            if next_token is None or next_token == "":
                result = list(items.values())
                pending = self._pending_creates.get(list_id)
                if pending and len([row for row in result if row.uid not in pending["before"]
                                   and row.fields() == pending["fields"]]) == 1:
                    # The engine can adopt this confirmed create during a read,
                    # without invoking add_item a second time.
                    self._pending_creates.pop(list_id, None)
                return result
            if not isinstance(next_token, str) or len(next_token) > 8192 or next_token in tokens:
                raise ConnectorError(_FORMAT_ERROR)
            tokens.add(next_token)
        raise ConnectorError(_FORMAT_ERROR)

    async def _current(self, list_id, item):
        current = next((row for row in await self.get_items(list_id) if row.uid == item.uid), None)
        if current is not None and (current.metadata["version"] != item.metadata.get("version")
                                    or current.fields() != item.fields()):
            raise ConnectorError("Der Alexa-Artikel wurde inzwischen geändert. Bitte die Liste erneut laden.")
        return current

    async def _confirm(self, list_id, uid, fields):
        current = next((row for row in await self.get_items(list_id) if row.uid == uid), None)
        if current is None or current.fields() != fields:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def _change(self, list_id, current, field, value):
        await self._wire("PUT", (list_id, "items", current.uid), query={"version": current.metadata["version"]},
                         body={"itemAttributesToUpdate": [{"type": field, "value": value}],
                               "itemAttributesToRemove": []}, read=False)

    async def _finish_create(self, list_id):
        pending = self._pending_creates[list_id]
        fields = pending["fields"]
        candidates = [row for row in await self.get_items(list_id) if row.uid not in pending["before"]
                      and (row.name, row.description) == (fields["name"], fields["description"])]
        if len(candidates) != 1:
            raise ConnectorError(_CONFIRM_ERROR)
        current = candidates[0]
        if current.completed != fields["completed"]:
            await self._change(list_id, current, "itemStatus", "COMPLETE" if fields["completed"] else "ACTIVE")
        item = await self._confirm(list_id, current.uid, fields)
        self._pending_creates.pop(list_id, None)
        return item

    @staticmethod
    def _fields(name, description, completed):
        if (not isinstance(name, str) or not name.strip() or not isinstance(description, str)
                or type(completed) is not bool or len(render_label(name, description)) > 4096):
            raise ConnectorError("Bitte einen gültigen Einkaufsartikel mit Namen und Mengenangabe eingeben.")
        return {"name": name, "description": description, "completed": completed}

    async def add_item(self, list_id: str, name: str, description: str = "", completed: bool = False) -> Item:
        fields = self._fields(name, description, completed)
        async with self._write_lock:
            if list_id in self._pending_creates:
                if self._pending_creates[list_id]["fields"] != fields:
                    raise ConnectorError(_CONFIRM_ERROR)
                return await self._finish_create(list_id)
            before = {row.uid for row in await self.get_items(list_id)}
            self._pending_creates[list_id] = {"fields": fields, "before": before}
            await self._wire("POST", (list_id, "items"), body={"items": [
                {"itemType": "KEYWORD", "itemName": render_label(name, description)}]}, read=False)
            return await self._finish_create(list_id)

    async def update_item(self, list_id: str, item: Item, *, name: str | None = None,
                          description: str | None = None, completed: bool | None = None) -> Item:
        async with self._write_lock:
            current = await self._current(list_id, item)
            if current is None:
                raise ConnectorError("Der Alexa-Artikel wurde inzwischen entfernt.")
            fields = self._fields(current.name if name is None else name,
                                  current.description if description is None else description,
                                  current.completed if completed is None else completed)
            if current.completed != fields["completed"]:
                await self._change(list_id, current, "itemStatus", "COMPLETE" if fields["completed"] else "ACTIVE")
                current = await self._confirm(list_id, current.uid, {**current.fields(), "completed": fields["completed"]})
            if (current.name, current.description) != (fields["name"], fields["description"]):
                await self._change(list_id, current, "itemName", render_label(fields["name"], fields["description"]))
            return await self._confirm(list_id, current.uid, fields)

    async def delete_item(self, list_id: str, item: Item) -> None:
        async with self._write_lock:
            current = await self._current(list_id, item)
            if current is None:
                return
            await self._wire("DELETE", (list_id, "items", current.uid), query={"version": current.metadata["version"]}, read=False)
            if any(row.uid == current.uid for row in await self.get_items(list_id)):
                raise ConnectorError(_CONFIRM_ERROR)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = self._api = None
