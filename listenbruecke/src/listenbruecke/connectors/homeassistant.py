"""Native Home Assistant REST adapter for exact to-do entities and item UIDs."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
import httpx

from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, ShoppingList, UnsupportedOperation
from listenbruecke.providers import PROVIDERS

CREATE, DELETE, UPDATE, DESCRIPTION = 1, 2, 4, 64
_FORMAT_ERROR = "Home Assistant hat ein unbekanntes Listenformat geliefert."
_CONFIRM_ERROR = "Home Assistant hat die Änderung noch nicht bestätigt. Bitte die Liste neu laden."
_LABEL_MARKER = " ⟦Menge: "
_ALIAS_PLATFORMS = {"bring", "monsieur_cuisine", "listenbruecke"}


def render_label(name: str, description: str) -> str:
    """Keep quantity text on entities without description support, reversibly."""
    if not description and _LABEL_MARKER not in name:
        return name
    escape = lambda s: s.replace("\\", "\\\\").replace("⟦", "\\⟦").replace("⟧", "\\⟧")
    return f"{escape(name)}{_LABEL_MARKER}{escape(description)}⟧"


def split_label(label: str) -> tuple[str, str]:
    position = label.find(_LABEL_MARKER)
    if position < 0 or not label.endswith("⟧"):
        return label, ""
    name, description = label[:position], label[position + len(_LABEL_MARKER):-1]
    unescape = lambda s: re.sub(r"\\([\\⟦⟧])", r"\1", s)
    # Accept only our own deterministic encoding, not an arbitrary partial label.
    pair = unescape(name), unescape(description)
    if render_label(*pair) == label:
        return pair
    return label, ""


class HomeAssistantConnector:
    def __init__(self, http: httpx.AsyncClient, url: str, token: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConnectorError("Bitte eine gültige Home-Assistant-Adresse eingeben.")
        if not isinstance(token, str) or not token:
            raise AuthenticationRequired("Bitte den Home-Assistant-Zugriff einrichten.")
        self.http, self.url, self.token = http, url.rstrip("/"), token
        self._write_lock = asyncio.Lock()
        self.origins: dict[str, str] = {}
        self.origins_resolved = False
        self._origins_checked_at = 0.0

    def _websocket_url(self) -> str:
        parsed = urlsplit(self.url)
        path = "/websocket" if parsed.hostname == "supervisor" and parsed.path.rstrip("/") == "/core" else "/api/websocket"
        return f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}{parsed.path.rstrip('/')}{path}"

    async def list_origins(self) -> dict[str, str]:
        """Read registry platforms to avoid syncing a provider to its HA mirror."""
        self.origins_resolved = False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.ws_connect(self._websocket_url(), timeout=aiohttp.ClientWSTimeout(ws_receive=15, ws_close=3), max_msg_size=8 * 1024 * 1024) as ws:
                    if (await ws.receive_json()).get("type") != "auth_required":
                        raise ValueError
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    auth = await ws.receive_json()
                    if auth.get("type") == "auth_invalid":
                        raise AuthenticationRequired("Der Home-Assistant-Zugriff ist ungültig. Bitte die Verbindung neu einrichten.")
                    if auth.get("type") != "auth_ok":
                        raise ValueError
                    await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
                    response = await ws.receive_json()
            if response.get("id") != 1 or response.get("success") is not True or not isinstance(response.get("result"), list):
                raise ValueError
            origins = {}
            for row in response["result"]:
                if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str) or not isinstance(row.get("platform"), str):
                    raise ValueError
                if row["entity_id"].startswith("todo."):
                    origins[row["entity_id"]] = row["platform"]
            self.origins = origins
            self.origins_resolved = True
            self._origins_checked_at = time.monotonic()
            return dict(origins)
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            raise ConnectorError("Die Herkunft der Home-Assistant-Listen konnte nicht geprüft werden. Bitte die Verbindung erneut testen.") from None

    async def _ensure_origins(self) -> None:
        if not self.origins_resolved or time.monotonic() - self._origins_checked_at > 300:
            await self.list_origins()

    async def check_independent_source(self, list_id: str, selected_providers: set[str]) -> None:
        """Allow HA alone; reject a second view of any selected direct provider."""
        await self._ensure_origins()
        if list_id not in self.origins:
            raise ConnectorError("Die Herkunft der ausgewählten Home-Assistant-Liste ist unbekannt.")
        for provider in selected_providers:
            spec = PROVIDERS.get(provider)
            if spec and self.origins[list_id] in spec.ha_platforms:
                raise UnsupportedOperation("Diese Home-Assistant-Liste ist bereits dieselbe " + spec.name +
                                           "-Liste. Bitte den Anbieter direkt oder über Home Assistant auswählen.")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(
                method, f"{self.url}/api/{path}",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=25, **kwargs,
            )
            if response.status_code in (401, 403):
                raise AuthenticationRequired("Der Home-Assistant-Zugriff ist ungültig. Bitte die Verbindung neu einrichten.")
            if response.status_code >= 400:
                raise ConnectorError("Home Assistant konnte die Listenanfrage nicht ausführen.")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise ConnectorError("Home Assistant ist nicht erreichbar oder hat ungültige Daten geliefert.") from None

    @staticmethod
    def _parse_state(state: Any, expected: str | None = None) -> tuple[str, str, int]:
        if not isinstance(state, dict) or not isinstance(state.get("attributes"), dict):
            raise ConnectorError(_FORMAT_ERROR)
        entity_id, attributes = state.get("entity_id"), state["attributes"]
        if not isinstance(entity_id, str) or not re.fullmatch(r"todo\.[a-z0-9_]+", entity_id) or (expected and entity_id != expected):
            raise ConnectorError(_FORMAT_ERROR)
        if state.get("state") in (None, "unavailable", "unknown"):
            raise ConnectorError("Die Home-Assistant-Liste ist momentan nicht verfügbar.")
        features = attributes.get("supported_features", 0)
        name = attributes.get("friendly_name", entity_id)
        if not isinstance(features, int) or isinstance(features, bool) or not isinstance(name, str):
            raise ConnectorError(_FORMAT_ERROR)
        return entity_id, name, features

    async def _state(self, list_id: str) -> tuple[str, str, int]:
        if not re.fullmatch(r"todo\.[a-z0-9_]+", list_id):
            raise ConnectorError("Bitte eine gültige Home-Assistant-To-do-Liste auswählen.")
        await self._ensure_origins()
        if self.origins.get(list_id) == "listenbruecke":
            raise UnsupportedOperation("Diese Home-Assistant-Liste ist bereits die gemeinsame Listenbrücke-Liste und kann nicht zusätzlich als Quelle verwendet werden.")
        if self.origins.get(list_id) in _ALIAS_PLATFORMS:
            raise UnsupportedOperation("Diese Home-Assistant-Liste ist bereits über Bring! oder Monsieur Cuisine verbunden. Bitte direkt beim Anbieter auswählen.")
        if list_id not in self.origins:
            raise ConnectorError("Die Herkunft der ausgewählten Home-Assistant-Liste ist unbekannt.")
        return self._parse_state(await self._request("GET", f"states/{quote(list_id, safe='')}"), list_id)

    async def test_connection(self) -> list[ShoppingList]:
        return await self.list_lists()

    async def list_lists(self) -> list[ShoppingList]:
        await self._ensure_origins()
        states = await self._request("GET", "states")
        if not isinstance(states, list):
            raise ConnectorError(_FORMAT_ERROR)
        result = []
        for state in states:
            if not isinstance(state, dict) or not isinstance(state.get("entity_id"), str):
                raise ConnectorError(_FORMAT_ERROR)
            if state["entity_id"].startswith("todo."):
                if self.origins.get(state["entity_id"]) in _ALIAS_PLATFORMS or state["entity_id"] not in self.origins:
                    continue
                # Unavailable lists must remain selectable for repair, but never
                # become an empty list in get_items().
                if state.get("state") in ("unavailable", "unknown"):
                    attrs = state.get("attributes")
                    if not isinstance(attrs, dict):
                        raise ConnectorError(_FORMAT_ERROR)
                    result.append(ShoppingList(state["entity_id"], str(attrs.get("friendly_name", state["entity_id"]))))
                else:
                    entity_id, name, _ = self._parse_state(state)
                    result.append(ShoppingList(entity_id, name))
        if len({i.id for i in result}) != len(result):
            raise ConnectorError(_FORMAT_ERROR)
        return result

    async def _items(self, list_id: str, features: int) -> list[Item]:
        response = await self._request("POST", "services/todo/get_items", params={"return_response": "true"},
                                       json={"entity_id": list_id, "status": ["needs_action", "completed"]})
        if not isinstance(response, dict) or not isinstance(response.get("service_response"), dict):
            raise ConnectorError(_FORMAT_ERROR)
        own = response["service_response"].get(list_id)
        if not isinstance(own, dict) or not isinstance(own.get("items"), list):
            raise ConnectorError(_FORMAT_ERROR)
        items = []
        for row in own["items"]:
            if (not isinstance(row, dict) or not isinstance(row.get("uid"), str) or not row["uid"] or
                    not isinstance(row.get("summary"), str) or not row["summary"].strip() or
                    row.get("status") not in ("needs_action", "completed") or
                    row.get("description") is not None and not isinstance(row["description"], str)):
                raise ConnectorError(_FORMAT_ERROR)
            if features & DESCRIPTION:
                name, description = row["summary"], row.get("description") or ""
            else:
                name, description = split_label(row["summary"])
            items.append(Item(row["uid"], name, description, row["status"] == "completed",
                              editable=bool(features & UPDATE), deletable=bool(features & DELETE),
                              metadata={"provider_label": row["summary"], "description_supported": bool(features & DESCRIPTION)}))
        if len({i.uid for i in items}) != len(items):
            raise ConnectorError(_FORMAT_ERROR)
        return items

    async def get_items(self, list_id: str) -> list[Item]:
        _, _, features = await self._state(list_id)
        return await self._items(list_id, features)

    @staticmethod
    def _require(features: int, feature: int) -> None:
        if not features & feature:
            raise UnsupportedOperation("Diese Home-Assistant-Liste unterstützt die gewünschte Änderung nicht.")

    @staticmethod
    def _find_exact(rows: list[Item], uid: str) -> Item:
        target = next((row for row in rows if row.uid == uid), None)
        if target is None:
            raise ConnectorError("Der Home-Assistant-Eintrag wurde inzwischen entfernt.")
        # HA service resolution accepts UID OR summary and checks in list order.
        # Refuse a pathological summary collision rather than mutate a neighbour.
        if any(row.uid != uid and row.metadata.get("provider_label", row.name) == uid for row in rows):
            raise ConnectorError("Die Home-Assistant-Liste enthält eine mehrdeutige Eintragskennung.")
        return target

    async def _confirm(self, list_id: str, uid: str, name: str, description: str, completed: bool) -> Item:
        rows = await self.get_items(list_id)
        current = next((i for i in rows if i.uid == uid), None)
        if current is None or current.fields() != {"name": name, "description": description, "completed": completed}:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def add_item(self, list_id: str, name: str, description: str = "", completed: bool = False) -> Item:
        if not name.strip():
            raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
        async with self._write_lock:
            _, _, features = await self._state(list_id)
            self._require(features, CREATE)
            if completed:
                self._require(features, UPDATE)
            before = {i.uid for i in await self._items(list_id, features)}
            payload = {"entity_id": list_id, "item": name if features & DESCRIPTION else render_label(name, description)}
            if features & DESCRIPTION:
                payload["description"] = description
            await self._request("POST", "services/todo/add_item", json=payload)
            rows = await self.get_items(list_id)
            created = [i for i in rows if i.uid not in before and i.name == name and i.description == description]
            if len(created) != 1:
                raise ConnectorError(_CONFIRM_ERROR)
            item = created[0]
            if completed:
                self._find_exact(rows, item.uid)
                await self._request("POST", "services/todo/update_item", json={"entity_id": list_id, "item": item.uid, "status": "completed"})
            return await self._confirm(list_id, item.uid, name, description, completed)

    async def update_item(self, list_id: str, item: Item, *, name: str | None = None,
                          description: str | None = None, completed: bool | None = None) -> Item:
        async with self._write_lock:
            _, _, features = await self._state(list_id)
            self._require(features, UPDATE)
            current = self._find_exact(await self._items(list_id, features), item.uid)
            new_name = current.name if name is None else name
            new_description = current.description if description is None else description
            new_completed = current.completed if completed is None else completed
            if not new_name.strip():
                raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
            payload = {"entity_id": list_id, "item": current.uid}
            if (new_name, new_description) != (current.name, current.description):
                payload["rename"] = new_name if features & DESCRIPTION else render_label(new_name, new_description)
                if features & DESCRIPTION:
                    payload["description"] = new_description
            if new_completed != current.completed:
                payload["status"] = "completed" if new_completed else "needs_action"
            if len(payload) > 2:
                await self._request("POST", "services/todo/update_item", json=payload)
            return await self._confirm(list_id, current.uid, new_name, new_description, new_completed)

    async def delete_item(self, list_id: str, item: Item) -> None:
        async with self._write_lock:
            _, _, features = await self._state(list_id)
            self._require(features, DELETE)
            current = self._find_exact(await self._items(list_id, features), item.uid)
            await self._request("POST", "services/todo/remove_item", json={"entity_id": list_id, "item": current.uid})
            if any(i.uid == current.uid for i in await self.get_items(list_id)):
                raise ConnectorError(_CONFIRM_ERROR)

    async def close(self) -> None:
        # HTTP pool is shared with the application and MC authentication.
        return None
