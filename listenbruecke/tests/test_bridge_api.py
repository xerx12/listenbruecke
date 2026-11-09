"""Scoped native HA API and migration tests without external services."""

import asyncio
import copy
import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from listenbruecke.bridge import TARGET, BridgeConnector
from listenbruecke.main import COOKIE, create_app
from listenbruecke.models import ConnectorError, Item, ShoppingList
from listenbruecke.store import Store
from listenbruecke.sync import SyncEngine


class FakeConnector:
    def __init__(self):
        self.items, self.calls = {}, []
        self.fail = False

    async def test_connection(self):
        return [ShoppingList("list", "Private list name")]

    async def get_items(self, list_id):
        self.calls.append("read")
        if self.fail:
            raise ConnectorError("Quelle ist nicht verfügbar.")
        return list(self.items.values())

    async def add_item(self, list_id, **fields):
        item = Item("new-" + str(len(self.items)), **fields)
        self.items[item.uid] = item
        self.calls.append("add")
        return item

    async def update_item(self, list_id, item, **fields):
        updated = replace(item, **fields)
        self.items[item.uid] = updated
        self.calls.append("update")
        return updated

    async def delete_item(self, list_id, item):
        self.items.pop(item.uid, None)
        self.calls.append("delete")

    async def close(self):
        pass


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    clients = {"bring": FakeConnector(), "homeassistant": FakeConnector()}
    async def factory(provider, credentials):
        return clients[provider]
    return tmp_path, clients, factory


def login(client):
    assert client.post("/api/setup", json={"password": "strong-test-password"}).status_code == 200


def pair(client):
    result = client.post("/api/integration/token", json={})
    assert result.status_code == 200, result.text
    return result.json(), {"authorization": "Bearer " + result.json()["token"]}


def configure(client, provider):
    credentials = ({"email": "synthetic@example.invalid", "password": "private-password"} if provider == "bring"
                   else {"url": "http://homeassistant.local:8123", "token": "private-access-token"})
    assert client.put("/api/connections/" + provider, json=credentials).status_code == 200
    assert client.post("/api/connections/" + provider + "/test", json={}).json()["connected"]


def run_sync(client, app, headers):
    response = client.post("/api/ha/v1/sync", json={}, headers=headers)
    assert response.status_code == 202, response.text
    async def finish():
        await app.state.service.manual_task
    client.portal.call(finish)


def test_token_is_hashed_once_rotated_scoped_and_machine_auth_ignores_only_machine_origin(environment):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=False)
    with TestClient(app) as client:
        assert client.get("/api/integration").status_code == 401
        login(client)
        assert client.get("/api/ha/v1/state").status_code == 401  # UI cookie is insufficient.
        response, headers = pair(client)
        config = app.state.service.store.get("ha_integration")
        assert config["token_hash"] == hashlib.sha256(response["token"].encode()).hexdigest()
        assert response["token"] not in str(config)
        state = client.get("/api/ha/v1/state", headers=headers).json()
        assert state["instance_id"] == response["instance_id"]
        assert "lists" not in str(state)
        public = client.get("/api/integration").json()
        assert public["last_seen"] and public["token_configured"]
        assert "token_hash" not in public and "token" not in public
        client.cookies.clear()
        for path in ("/api/status", "/api/integration", "/api/settings", "/api/backup/summary", "/api/events"):
            assert client.get(path, headers=headers).status_code == 401, path
        internal = {**headers, "host": "a0d7b954-listenbruecke", "origin": "http://native-ha-host"}
        assert client.post("/api/ha/v1/items", json={"summary": "Milch"}, headers=internal).status_code == 200
        assert client.get("/api/status", headers=internal).status_code == 403
        assert client.post("/api/ha/v1/items", json={}, headers={**internal, "authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/login", json={"password": "strong-test-password"}).status_code == 200
        new_response, new_headers = pair(client)
        assert new_response["instance_id"] == response["instance_id"]
        assert client.get("/api/ha/v1/state", headers=headers).status_code == 401
        assert client.get("/api/ha/v1/state", headers=new_headers).status_code == 200


def test_local_crud_duplicates_idempotency_restart_and_no_cloud_reads(environment):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=False)
    with TestClient(app) as client:
        login(client)
        response, headers = pair(client)
        first = client.post("/api/ha/v1/items", json={"summary": "USB-C", "description": "2 STÜCK", "request_id": "one"}, headers=headers).json()["item"]
        again = client.post("/api/ha/v1/items", json={"summary": "USB-C", "description": "2 STÜCK", "request_id": "one"}, headers=headers).json()["item"]
        assert first == again
        duplicate = client.post("/api/ha/v1/items", json={"summary": "USB-C", "description": "2 STÜCK"}, headers=headers).json()["item"]
        assert duplicate["uid"] != first["uid"]
        assert client.post("/api/ha/v1/items", json={"summary": "Andere", "request_id": "one"}, headers=headers).status_code == 400
        changed = client.patch("/api/ha/v1/items/" + first["uid"], json={"status": "completed", "summary": "USB-C ÖL"}, headers=headers).json()["item"]
        assert changed["description"] == "2 STÜCK" and changed["status"] == "completed"
        assert client.request("DELETE", "/api/ha/v1/items/" + duplicate["uid"], json={}, headers=headers).json()["ok"]
        assert client.get("/api/ha/v1/items", headers=headers).json() == {"available": True, "items": [changed]}
        assert not any(connector.calls for connector in clients.values())
        assert not app.state.service.settings()["enabled"]
        assert app.state.service.wakeup.is_set()
    restarted = create_app(directory, factory=factory, background=False)
    with TestClient(restarted) as client:
        assert client.get("/api/ha/v1/items", headers=headers).json()["items"] == [changed]
        assert client.get("/api/ha/v1/state", headers=headers).json()["instance_id"] == response["instance_id"]


@pytest.mark.parametrize("deletions", [False, True])
def test_local_items_sync_both_directions_completion_and_optional_deletions(environment, deletions):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=False)
    with TestClient(app) as client:
        login(client)
        configure(client, "bring")
        _, headers = pair(client)
        settings = {"enabled": False, "targets": [{"provider": "bring", "list_id": "list"}], "propagate_deletions": deletions, "interval_seconds": 120}
        assert client.put("/api/settings", json=settings).status_code == 200
        created = client.post("/api/ha/v1/items", json={"summary": "Milch", "description": "½ L"}, headers=headers).json()["item"]
        run_sync(client, app, headers)
        assert len(clients["bring"].items) == 1
        remote = next(iter(clients["bring"].items.values()))
        assert remote.description == "½ L"
        clients["bring"].items[remote.uid] = replace(remote, completed=True)
        run_sync(client, app, headers)
        assert client.get("/api/ha/v1/items", headers=headers).json()["items"][0]["status"] == "completed"
        assert client.patch("/api/ha/v1/items/" + created["uid"], json={"status": "needs_action"}, headers=headers).status_code == 200
        run_sync(client, app, headers)
        assert not clients["bring"].items[remote.uid].completed
        assert client.request("DELETE", "/api/ha/v1/items/" + created["uid"], json={}, headers=headers).status_code == 200
        run_sync(client, app, headers)
        assert bool(clients["bring"].items) is not deletions
        run_sync(client, app, headers)
        assert not client.get("/api/ha/v1/items", headers=headers).json()["items"]


def test_partial_machine_settings_preserve_targets_and_reject_busy_restore(environment):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=False)
    with TestClient(app) as client:
        login(client)
        configure(client, "bring")
        _, headers = pair(client)
        settings = {"enabled": False, "targets": [{"provider": "bring", "list_id": "list"}], "propagate_deletions": True, "interval_seconds": 120}
        client.put("/api/settings", json=settings)
        assert client.put("/api/ha/v1/settings", json={"enabled": True}, headers=headers).status_code == 200
        assert client.get("/api/settings").json() == {**settings, "enabled": True}
        assert client.put("/api/ha/v1/settings", json={"interval_seconds": True}, headers=headers).status_code == 400
        assert client.put("/api/ha/v1/settings", json={"targets": []}, headers=headers).status_code == 400
        client.portal.call(app.state.service.lock.acquire)
        try:
            assert client.post("/api/ha/v1/items", json={"summary": "Brot"}, headers=headers).status_code == 409
        finally:
            client.portal.call(app.state.service.lock.release)
        app.state.service.restore_pending = True
        assert not client.get("/api/ha/v1/items", headers=headers).json()["available"]
        assert client.post("/api/ha/v1/sync", json={}, headers=headers).status_code == 409


def test_pairing_existing_group_imports_baselines_without_external_copies_or_history(environment):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=False)
    with TestClient(app) as client:
        login(client)
        for provider in clients:
            configure(client, provider)
        targets = [{"provider": provider, "list_id": "list"} for provider in clients]
        assert client.put("/api/settings", json={"targets": targets}).status_code == 200
        clients["bring"].items = {"first": Item("first", "Milch"), "second": Item("second", "Milch"),
                                  "history": Item("history", "Alt", completed=True)}
        async def finish():
            await app.state.service.manual_task
        assert client.post("/api/sync", json={}).status_code == 202
        client.portal.call(finish)
        clients["bring"].items["first"] = replace(clients["bring"].items["first"], completed=True)
        assert client.post("/api/sync", json={}).status_code == 202
        client.portal.call(finish)
        originals = {provider: copy.deepcopy(connector.items) for provider, connector in clients.items()}
        original = app.state.service.store.get(SyncEngine(app.state.service.store).state_key(targets))
        _, headers = pair(client)
        new_key = SyncEngine(app.state.service.store).state_key([*targets, TARGET])
        assert app.state.service.store.get(new_key) == original
        run_sync(client, app, headers)
        items = client.get("/api/ha/v1/items", headers=headers).json()["items"]
        assert len(items) == 2 and all(item["summary"] == "Milch" for item in items)
        assert sum(item["status"] == "completed" for item in items) == 1
        assert {provider: connector.items for provider, connector in clients.items()} == originals
        # Readability and safe local mutations remain available through outages.
        clients["bring"].fail = True
        run_sync(client, app, headers)
        assert client.get("/api/ha/v1/items", headers=headers).json()["items"] == items


def test_local_mutations_do_not_resume_paused_background_sync(environment):
    directory, clients, factory = environment
    app = create_app(directory, factory=factory, background=True)
    with TestClient(app) as client:
        login(client)
        configure(client, "bring")
        _, headers = pair(client)
        assert client.put("/api/settings", json={"enabled": False, "targets": [{"provider": "bring", "list_id": "list"}]}).status_code == 200
        assert client.post("/api/ha/v1/items", json={"summary": "Milch"}, headers=headers).status_code == 200
        async def worker_turn():
            await asyncio.sleep(0.01)
        client.portal.call(worker_turn)
        assert not clients["bring"].calls
        assert not client.get("/api/ha/v1/state", headers=headers).json()["sync"]["enabled"]


@pytest.mark.asyncio
async def test_group_extension_preserves_every_baseline_ignored_history_and_intention(tmp_path):
    store = Store(tmp_path)
    try:
        engine = SyncEngine(store)
        targets = [{"provider": "a", "list_id": "list"}, {"provider": "b", "list_id": "list"}]
        a, b = FakeConnector(), FakeConnector()
        a.items = {"active": Item("active", "Milch"), "historic": Item("historic", "Alt", completed=True)}
        await engine.run(targets, {"a": a, "b": b})
        a.items["active"] = replace(a.items["active"], completed=True)
        await engine.run(targets, {"a": a, "b": b})
        state = store.get(engine.state_key(targets))
        record = next(iter(state["records"].values()))
        record["updating"] = {"b:list": {"uid": next(iter(b.items)), "before": list(b.items), "fields": record["fields"]}}
        store.set(engine.state_key(targets), state)
        original = copy.deepcopy(state)
        engine.include_target(targets, TARGET)
        destination = engine.state_key([*targets, TARGET])
        assert store.get(destination) == original
        local = BridgeConnector(store)
        result = await engine.run([*targets, TARGET], {"a": a, "b": b, "_bridge": local})
        assert result["added"] == 1
        assert [item.name for item in await local.get_items("local")] == ["Milch"]
        assert (await local.get_items("local"))[0].completed
        assert len(a.items) == 2 and len(b.items) == 1
        saved = store.get(destination)
        engine.include_target(targets, TARGET)
        assert store.get(destination) == saved  # Rotation/retry never overwrites newer state.
        assert "historic" in saved["ignored"]["a:list"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pending_create_is_recovered_after_bridge_activation_without_remote_duplicate(tmp_path):
    store = Store(tmp_path)
    try:
        targets = [{"provider": "a", "list_id": "list"}, {"provider": "b", "list_id": "list"}]
        engine, a, b = SyncEngine(store), FakeConnector(), FakeConnector()
        a.items["a"] = Item("a", "Milch")
        original_add = b.add_item
        async def interrupted(list_id, **fields):
            await original_add(list_id, **fields)
            raise ConnectorError("Antwort nach dem Schreiben unterbrochen.")
        b.add_item = interrupted
        with pytest.raises(ConnectorError):
            await engine.run(targets, {"a": a, "b": b})
        previous = store.get(engine.state_key(targets))
        assert next(iter(previous["records"].values()))["creating"]
        engine.include_target(targets, TARGET)
        assert store.get(engine.state_key([*targets, TARGET])) == previous
        b.add_item = original_add
        local = BridgeConnector(store)
        assert (await engine.run([*targets, TARGET], {"a": a, "b": b, "_bridge": local}))["added"] == 1
        assert len(a.items) == len(b.items) == len(await local.get_items("local")) == 1
        assert b.calls.count("add") == 1
    finally:
        store.close()


def test_ingress_can_issue_token_but_cannot_use_machine_routes_without_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-supervisor-token")
    monkeypatch.setattr("listenbruecke.bridge.socket.gethostname", lambda: "abc123-listenbruecke")
    app = create_app(tmp_path, background=False)
    with TestClient(app, client=("172.30.32.2", 1234)) as client:
        ingress = {"x-ingress-path": "/api/hassio_ingress/synthetic-path"}
        assert client.get("/api/ha/v1/state", headers=ingress).status_code == 401
        result = client.post("/api/integration/token", json={}, headers=ingress)
        assert result.status_code == 200
        assert result.json()["suggested_url"] == "http://abc123-listenbruecke:8099"
        headers = {"authorization": "Bearer " + result.json()["token"]}
        state = client.get("/api/ha/v1/state", headers=headers)
        assert state.status_code == 200
        assert "synthetic-supervisor-token" not in state.text
