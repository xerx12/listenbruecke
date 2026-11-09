"""Persistent local shopping list used by the scoped Home Assistant API."""

import copy
import hashlib
import secrets
import socket
import uuid

from listenbruecke.models import ConnectorError, Item, ShoppingList

TARGET = {"provider": "_bridge", "list_id": "local"}
_KEY = "bridge_items"


class BridgeConnector:
    def __init__(self, store):
        self.store = store

    def _state(self):
        return self.store.get(_KEY, {"items": {}, "requests": {}})

    @staticmethod
    def _item(uid, fields):
        return Item(uid, **fields)

    @staticmethod
    def fields(name, description="", completed=False):
        if (not isinstance(name, str) or not name.strip() or len(name) > 4096
                or not isinstance(description, str) or len(description) > 8192
                or type(completed) is not bool):
            raise ConnectorError("Bitte einen gültigen Artikelnamen und eine gültige Mengenangabe eingeben.")
        return {"name": name, "description": description, "completed": completed}

    async def test_connection(self):
        return [ShoppingList("local", "Listenbrücke")]

    async def get_items(self, list_id):
        if list_id != "local":
            raise ConnectorError("Diese lokale Liste ist nicht bekannt.")
        return [self._item(uid, fields) for uid, fields in self._state()["items"].items()]

    async def add_item(self, list_id, name, description="", completed=False, *, request_id=None):
        if list_id != "local":
            raise ConnectorError("Diese lokale Liste ist nicht bekannt.")
        fields = self.fields(name, description, completed)
        state = self._state()
        if request_id is not None:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
                raise ConnectorError("Bitte eine gültige Anfragekennung übergeben.")
            previous = state["requests"].get(request_id)
            if previous:
                if previous["fields"] != fields:
                    raise ConnectorError("Diese Anfragekennung wurde bereits für einen anderen Artikel verwendet.")
                item = state["items"].get(previous["uid"])
                if item is None:
                    raise ConnectorError("Der Artikel dieser Anfrage wurde inzwischen entfernt.")
                return self._item(previous["uid"], item)
        uid = str(uuid.uuid4())
        state["items"][uid] = fields
        if request_id is not None:
            state["requests"][request_id] = {"uid": uid, "fields": copy.deepcopy(fields)}
        self.store.set(_KEY, state)
        return self._item(uid, fields)

    async def update_item(self, list_id, item, **values):
        if list_id != "local":
            raise ConnectorError("Diese lokale Liste ist nicht bekannt.")
        state = self._state()
        current = state["items"].get(item.uid)
        if current is None or current != item.fields():
            raise ConnectorError("Der lokale Artikel wurde inzwischen geändert oder entfernt.")
        fields = self.fields(**{**current, **values})
        state["items"][item.uid] = fields
        self.store.set(_KEY, state)
        return self._item(item.uid, fields)

    async def delete_item(self, list_id, item):
        if list_id != "local":
            raise ConnectorError("Diese lokale Liste ist nicht bekannt.")
        state = self._state()
        current = state["items"].get(item.uid)
        if current is not None and current != item.fields():
            raise ConnectorError("Der lokale Artikel wurde inzwischen geändert.")
        state["items"].pop(item.uid, None)
        self.store.set(_KEY, state)

    async def close(self):
        pass


def integration_config(store):
    config = store.get("ha_integration", {})
    if not config.get("instance_id"):
        config = {**config, "instance_id": str(uuid.uuid4())}
        store.set("ha_integration", config)
    return config


def integration_status(store, *, addon=False):
    config = integration_config(store)
    return {"enabled": config.get("enabled", False), "instance_id": config["instance_id"],
            "suggested_url": f"http://{socket.gethostname()}:8099" if addon else None,
            "token_configured": bool(config.get("token_hash")), "last_seen": config.get("last_seen")}


def issue_token(store):
    config = integration_config(store)
    token = secrets.token_urlsafe(32)
    config.update(enabled=True, token_hash=hashlib.sha256(token.encode()).hexdigest())
    store.set("ha_integration", config)
    return token


def valid_token(store, authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    if not 32 <= len(token) <= 256:
        return False
    config = store.get("ha_integration", {})
    expected = config.get("token_hash")
    return bool(config.get("enabled") and isinstance(expected, str)
                and secrets.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest()))


def machine_item(item):
    return {"uid": item.uid, "summary": item.name, "description": item.description,
            "status": "completed" if item.completed else "needs_action"}
