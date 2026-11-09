"""Authenticated local app API; never log tokens or response bodies."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiohttp


class ApiError(Exception):
    """A fixed, safe local API error."""


class ApiAuthError(ApiError):
    """The dedicated Home Assistant pairing token is invalid."""


def normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(part in (".", "..") for part in parsed.path.split("/"))):
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError("Die Adresse der Listenbrücke-App ist ungültig.") from None
    return value.rstrip("/")


def _string(value: Any, *, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ApiError("Die Listenbrücke-App hat ein unbekanntes Datenformat geliefert.")
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def parse_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in ("needs_action", "completed"):
        raise ApiError("Die Einkaufsliste hat ein unbekanntes Datenformat.")
    return {"uid": _string(value.get("uid"), empty=False),
            "summary": _string(value.get("summary"), empty=False),
            "description": _string(value.get("description", "")), "status": value["status"]}


class ListenbrueckeApi:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        self.session = session
        self.base_url = normalize_url(base_url)
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ApiAuthError("Bitte ein gültiges Home-Assistant-App-Token eingeben.")
        self._token = token

    async def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        # Mutations are deliberately attempted once. A lost response must never
        # turn into an automatic second creation or deletion.
        try:
            async with self.session.request(
                method, self.base_url + "/api/ha/v1/" + path,
                headers={"Authorization": "Bearer " + self._token}, json=data,
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise ApiAuthError("Das Home-Assistant-App-Token ist ungültig. Bitte erneut verbinden.")
                if response.status < 200 or response.status >= 300:
                    raise ApiError("Die Listenbrücke-App konnte die Anfrage nicht ausführen.")
                body = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise ApiError("Die Listenbrücke-App ist nicht erreichbar oder hat ungültige Daten geliefert.") from None
        if not isinstance(body, dict):
            raise ApiError("Die Listenbrücke-App hat ein unbekanntes Datenformat geliefert.")
        return body

    async def async_get_state(self) -> dict:
        data = await self._request("GET", "state")
        sync = data.get("sync")
        providers = data.get("providers")
        if (not isinstance(sync, dict) or type(sync.get("enabled")) is not bool
                or type(sync.get("running")) is not bool or type(sync.get("interval_seconds")) is not int
                or not 30 <= sync["interval_seconds"] <= 3600
                or type(sync.get("items_synced")) is not int or sync["items_synced"] < 0
                or not isinstance(providers, list)):
            raise ApiError("Die Listenbrücke-App hat ein unbekanntes Datenformat geliefert.")
        parsed_providers = []
        for provider in providers:
            if (not isinstance(provider, dict) or type(provider.get("configured")) is not bool
                    or type(provider.get("connected")) is not bool):
                raise ApiError("Die Listenbrücke-App hat ein unbekanntes Datenformat geliefert.")
            parsed_providers.append({
                "id": _string(provider.get("id"), empty=False), "name": _string(provider.get("name"), empty=False),
                "configured": provider["configured"], "connected": provider["connected"],
                "last_test": _optional_string(provider.get("last_test")), "error": _optional_string(provider.get("error")),
            })
        if len({provider["id"] for provider in parsed_providers}) != len(parsed_providers):
            raise ApiError("Die Listenbrücke-App hat mehrdeutige Anbieterkennungen geliefert.")
        return {"instance_id": _string(data.get("instance_id"), empty=False),
                "name": _string(data.get("name"), empty=False), "version": _string(data.get("version"), empty=False),
                "sync": {"enabled": sync["enabled"], "interval_seconds": sync["interval_seconds"],
                         "running": sync["running"], "last_success": _optional_string(sync.get("last_success")),
                         "last_error": _optional_string(sync.get("last_error")), "items_synced": sync["items_synced"]},
                "providers": parsed_providers}

    async def async_get_items(self) -> dict:
        data = await self._request("GET", "items")
        if type(data.get("available")) is not bool or not isinstance(data.get("items"), list):
            raise ApiError("Die Einkaufsliste hat ein unbekanntes Datenformat.")
        items = [parse_item(value) for value in data["items"]]
        if len({item["uid"] for item in items}) != len(items):
            raise ApiError("Die Einkaufsliste enthält mehrdeutige Eintragskennungen.")
        return {"available": data["available"], "items": items}

    async def async_add_item(self, summary: str, description: str = "") -> dict:
        body = await self._request("POST", "items", {"summary": summary, "description": description, "request_id": str(uuid4())})
        return parse_item(body.get("item"))

    async def async_update_item(self, uid: str, **fields: Any) -> dict:
        body = await self._request("PATCH", "items/" + quote(uid, safe=""), fields)
        item = parse_item(body.get("item"))
        if item["uid"] != uid:
            raise ApiError("Die Listenbrücke-App hat eine andere Eintragskennung bestätigt.")
        return item

    async def async_delete_item(self, uid: str) -> None:
        body = await self._request("DELETE", "items/" + quote(uid, safe=""), {})
        if body.get("ok") is not True:
            raise ApiError("Die Listenbrücke-App hat das Entfernen nicht bestätigt.")

    async def async_update_settings(self, **fields: Any) -> None:
        await self._request("PUT", "settings", fields)

    async def async_sync(self) -> None:
        await self._request("POST", "sync", {})
