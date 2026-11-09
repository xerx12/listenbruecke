"""Native HA transport mocks, with exact entity/UID and feature validation."""

import json
import time
from copy import deepcopy

import httpx
import pytest
import pytest_asyncio

from listenbruecke.connectors import homeassistant as module
from listenbruecke.connectors.homeassistant import HomeAssistantConnector, render_label, split_label
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, UnsupportedOperation


class FakeHA:
    def __init__(self):
        self.features = 71
        self.state = "2"
        self.rows = [{"uid": "uuid-a", "summary": "Milch", "description": "1 l", "status": "needs_action"},
                     {"uid": "uuid-b", "summary": "Milch", "description": "2 l", "status": "completed"}]
        self.calls = []
        self.bad_response = False
        self.http_status = 200

    def entity(self):
        return {"entity_id": "todo.local", "state": self.state,
                "attributes": {"friendly_name": "Lokal", "supported_features": self.features}}

    def handle(self, request):
        self.calls.append(request)
        if self.http_status != 200:
            return httpx.Response(self.http_status, json={"secret": "SYNTHETIC_REMOTE_BODY"})
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[self.entity(), {**self.entity(), "entity_id": "todo.bring_mirror"}])
        if request.url.path == "/api/states/todo.local":
            return httpx.Response(200, json=self.entity())
        payload = json.loads(request.content)
        assert payload["entity_id"] == "todo.local"
        if request.url.path.endswith("get_items"):
            assert request.url.params["return_response"] == "true"
            assert payload["status"] == ["needs_action", "completed"]
            body = {} if self.bad_response else {"service_response": {"todo.local": {"items": deepcopy(self.rows)}}}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("add_item"):
            self.rows.append({"uid": "uuid-c", "summary": payload["item"], "description": payload.get("description"), "status": "needs_action"})
        elif request.url.path.endswith("update_item"):
            row = next(i for i in self.rows if i["uid"] == payload["item"])
            for key, dest in (("rename", "summary"), ("description", "description"), ("status", "status")):
                if key in payload:
                    row[dest] = payload[key]
        elif request.url.path.endswith("remove_item"):
            self.rows = [i for i in self.rows if i["uid"] != payload["item"]]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json=[])


@pytest_asyncio.fixture
async def setup():
    backend = FakeHA()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handle)) as http:
        connector = HomeAssistantConnector(http, "http://homeassistant.invalid", "synthetic-token")
        connector.origins = {"todo.local": "local_todo", "todo.bring_mirror": "bring"}
        connector.origins_resolved = True
        connector._origins_checked_at = time.monotonic()
        yield connector, backend


@pytest.mark.asyncio
async def test_discovers_only_independent_lists_and_preserves_duplicate_items(setup):
    connector, backend = setup
    assert [(l.id, l.name) for l in await connector.list_lists()] == [("todo.local", "Lokal")]
    rows = await connector.get_items("todo.local")
    assert len(rows) == 2 and rows[0].uid != rows[1].uid
    assert rows[1].completed and rows[1].description == "2 l"
    with pytest.raises(UnsupportedOperation):
        await connector.get_items("todo.bring_mirror")
    assert not any("bring_mirror" in str(r.url) for r in backend.calls)


@pytest.mark.asyncio
async def test_crud_uses_exact_uid_and_selected_entity(setup):
    connector, backend = setup
    first, second = await connector.get_items("todo.local")
    changed = await connector.update_item("todo.local", second, name="Haferdrink", completed=False)
    assert changed.uid == second.uid and not changed.completed
    assert backend.rows[0]["summary"] == first.name
    writes = [json.loads(r.content) for r in backend.calls if r.url.path.endswith("update_item")]
    assert writes == [{"entity_id": "todo.local", "item": "uuid-b", "rename": "Haferdrink", "description": "2 l", "status": "needs_action"}]
    added = await connector.add_item("todo.local", "Brot", "1 Stück", completed=True)
    assert added.uid == "uuid-c" and added.completed
    await connector.delete_item("todo.local", first)
    assert {i.uid for i in await connector.get_items("todo.local")} == {"uuid-b", "uuid-c"}


@pytest.mark.asyncio
async def test_description_feature_falls_back_to_reversible_visible_quantity(setup):
    connector, backend = setup
    backend.features = 7
    backend.rows = []
    added = await connector.add_item("todo.local", "Mehl", "½ kg")
    assert added.name == "Mehl" and added.description == "½ kg"
    assert backend.rows[0]["summary"] == "Mehl ⟦Menge: ½ kg⟧"
    payload = next(json.loads(r.content) for r in backend.calls if r.url.path.endswith("add_item"))
    assert "description" not in payload
    updated = await connector.update_item("todo.local", added, description="1 kg")
    assert updated.description == "1 kg"


@pytest.mark.parametrize("name,description", [("Mehl", "½ kg"), ("N ⟦Menge: alt⟧", "X \\ ⟦Y⟧"), ("N ⟦Menge: alt⟧", ""), ("Milch", "")])
def test_quantity_label_roundtrip(name, description):
    assert split_label(render_label(name, description)) == (name, description)


@pytest.mark.asyncio
async def test_unavailable_invalid_schema_and_unsupported_feature_never_write(setup):
    connector, backend = setup
    backend.state = "unavailable"
    with pytest.raises(ConnectorError, match="verfügbar"):
        await connector.get_items("todo.local")
    assert not any(r.method == "POST" for r in backend.calls)
    backend.state = "2"
    backend.bad_response = True
    with pytest.raises(ConnectorError, match="Listenformat"):
        await connector.get_items("todo.local")
    backend.bad_response = False
    backend.features = 0
    with pytest.raises(UnsupportedOperation):
        await connector.add_item("todo.local", "Brot")


@pytest.mark.asyncio
async def test_wrong_entity_stale_uid_and_uid_summary_collision_refuse_writes(setup):
    connector, backend = setup
    with pytest.raises(ConnectorError):
        await connector.get_items("shopping_list.other")
    with pytest.raises(ConnectorError):
        await connector.delete_item("todo.local", Item("stale", "Milch"))
    backend.rows[0]["summary"] = "uuid-b"
    with pytest.raises(ConnectorError, match="mehrdeutige"):
        await connector.update_item("todo.local", Item("uuid-b", "Milch"), completed=False)
    assert not any(r.url.path.endswith(("remove_item", "update_item", "add_item")) for r in backend.calls)


@pytest.mark.asyncio
async def test_auth_error_sanitized(setup):
    connector, backend = setup
    backend.http_status = 401
    with pytest.raises(AuthenticationRequired) as exc:
        await connector.get_items("todo.local")
    assert "SYNTHETIC_REMOTE_BODY" not in str(exc.value)


class FakeWS:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def receive_json(self):
        return next(self.messages)
    async def send_json(self, value):
        self.sent.append(value)


@pytest.mark.asyncio
async def test_registry_platform_read_excludes_empty_bring_mirror(monkeypatch):
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"},
                 {"id": 1, "success": True, "result": [
                     {"entity_id": "todo.local", "platform": "local_todo"},
                     {"entity_id": "todo.bring_mirror", "platform": "bring"},
                 ]}])
    urls = []
    class Session:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        def ws_connect(self, url, **kwargs):
            urls.append(url)
            return ws
    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    backend = FakeHA()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handle)) as http:
        connector = HomeAssistantConnector(http, "http://supervisor/core", "synthetic-token")
        assert await connector.list_origins() == {"todo.local": "local_todo", "todo.bring_mirror": "bring"}
        assert connector.origins_resolved
        assert urls == ["ws://supervisor/core/websocket"]
        assert ws.sent[1] == {"id": 1, "type": "config/entity_registry/list"}


@pytest.mark.asyncio
async def test_registry_failure_is_not_an_empty_origin_map(monkeypatch):
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}, {"id": 1, "success": False}])
    class Session:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        def ws_connect(self, *args, **kwargs):
            return ws
    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    async with httpx.AsyncClient() as http:
        connector = HomeAssistantConnector(http, "https://ha.invalid", "synthetic-token")
        with pytest.raises(ConnectorError, match="Herkunft"):
            await connector.list_lists()
        assert not connector.origins_resolved
