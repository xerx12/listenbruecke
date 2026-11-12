"""Google Keep's real node/sync implementation against an in-memory server only."""

import asyncio
from copy import deepcopy
import logging
import threading
import time
from types import SimpleNamespace as NS

import gkeepapi
import pytest
import pytest_asyncio
import requests

from listenbruecke.connectors import google_keep as module
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item, UnsupportedOperation


class Server:
    def __init__(self):
        source = gkeepapi.Keep()
        self.grocery = source.createList("Einkauf", [("Milch", False), ("Milch", True)])
        self.other = source.createList("Einkauf", [("Privater Unterpunkt", False)])
        self.text_note = source.createNote("Textnotiz", "Unveränderte fremde Notiz")
        self.trash = source.createList("Papierkorb", [])
        self.trash.trash()
        self.deleted = source.createList("Gelöscht", [])
        self.deleted.delete()
        self.archived = source.createList("Archiviert", [])
        self.archived.archived = True
        self.rows = {}
        for node in source._findDirtyNodes():
            raw = node.save()
            raw["serverId"] = "server-" + node.id
            self.rows[node.id] = raw
        self.calls = []
        self.auth = []
        self.clients = []
        self.drop_writes = False
        self.failure = None

    def changes(self, target_version=None, nodes=None, labels=None):
        self.calls.append(deepcopy(nodes or []))
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        changed = []
        for node in nodes or []:
            if self.drop_writes:
                continue
            node = deepcopy(node)
            if gkeepapi.node.from_json(node).deleted:
                self.rows.pop(node["id"], None)
                changed.append({"id": node["id"]})
            else:
                node["serverId"] = "server-" + node["id"]
                self.rows[node["id"]] = node
                changed.append(node)
        return {"nodes": deepcopy(list(self.rows.values()) if target_version is None else changed),
                "toVersion": str(len(self.calls)), "truncated": False}

    def client(self, email, token, check_deadline):
        check_deadline()
        self.auth.append((email, token))
        client = gkeepapi.Keep()
        client._keep_api.changes = self.changes
        client.sync()
        self.clients.append(client)
        return client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("External requests are forbidden in synthetic tests")
    monkeypatch.setattr(requests.Session, "request", fail)


@pytest.fixture
def server(monkeypatch):
    server = Server()
    monkeypatch.setattr(module, "_new_client", server.client)
    return server


@pytest_asyncio.fixture
async def connector(server):
    connector = module.GoogleKeepConnector({"email": "synthetic@example.invalid", "master_token": "synthetic-token"})
    yield connector
    await connector.close()


async def test_token_login_once_only_checklists_and_identical_titles_stay_distinct(connector, server):
    lists = await connector.test_connection()
    assert [(row.id, row.name) for row in lists] == [(server.grocery.id, "Einkauf"), (server.other.id, "Einkauf")]
    rows = await connector.get_items(server.grocery.id)
    assert len(rows) == 2 and len({row.uid for row in rows}) == 2
    assert {(row.name, row.completed) for row in rows} == {("Milch", True), ("Milch", False)}
    assert server.auth == [("synthetic@example.invalid", "synthetic-token")]


async def test_crud_exact_native_ids_duplicate_names_and_quantity_round_trip(connector, server):
    untouched = deepcopy({key: row for key, row in server.rows.items()
                          if key not in {server.grocery.id, *(item.id for item in server.grocery.items)}})
    first, second = await connector.get_items(server.grocery.id)
    added = await connector.add_item(server.grocery.id, "Hafer ⟦Menge: Test⟧", "2½ l \\ Bio", True)
    assert added.uid not in {first.uid, second.uid}
    assert added.fields() == {"name": "Hafer ⟦Menge: Test⟧", "description": "2½ l \\ Bio", "completed": True}
    updated = await connector.update_item(server.grocery.id, second, name="Haferdrink", description="1 l", completed=False)
    assert updated.uid == second.uid and updated.fields() == {"name": "Haferdrink", "description": "1 l", "completed": False}
    await connector.delete_item(server.grocery.id, first)
    assert {row.uid for row in await connector.get_items(server.grocery.id)} == {second.uid, added.uid}
    assert {key: server.rows[key] for key in untouched} == untouched
    assert all(not row.get("title") or row["id"] == server.grocery.id for call in server.calls for row in call)


async def test_write_resync_reads_from_server_not_sdk_locally_cleaned_values(connector, server):
    item = (await connector.get_items(server.grocery.id))[0]
    server.drop_writes = True
    with pytest.raises(ConnectorError, match="noch nicht bestätigt"):
        await connector.update_item(server.grocery.id, item, name="Nicht gespeichert")
    assert connector._client is None
    writes = [call for call in server.calls if call]
    assert len(writes) == 1
    assert (await connector.get_items(server.grocery.id))[0].name == "Milch"
    assert len([call for call in server.calls if call]) == 1


@pytest.mark.parametrize("operation", ["add", "delete"])
async def test_unacknowledged_add_and_delete_are_not_reported_successful(connector, server, operation):
    item = (await connector.get_items(server.grocery.id))[0]
    server.drop_writes = True
    with pytest.raises(ConnectorError, match="noch nicht bestätigt"):
        if operation == "add":
            await connector.add_item(server.grocery.id, "Unsichtbar")
        else:
            await connector.delete_item(server.grocery.id, item)
    assert connector._client is None
    assert len([call for call in server.calls if call]) == 1


async def test_fresh_sync_precedes_write_and_preserves_remote_changes(connector, server):
    item = (await connector.get_items(server.grocery.id))[0]
    # Emulate another client changing this item between our reads. An initial
    # full refresh exposes the changed fields before the requested checkbox edit.
    server.rows[item.uid]["text"] = "Aktueller Name"
    original = server.changes
    first = True
    def changes(target_version=None, nodes=None, labels=None):
        nonlocal first
        result = original(target_version, nodes, labels)
        if first:
            result["nodes"] = deepcopy(list(server.rows.values()))
            first = False
        return result
    connector._client._keep_api.changes = changes
    updated = await connector.update_item(server.grocery.id, item, completed=True)
    assert updated.name == "Aktueller Name" and updated.completed
    assert server.calls[-3] == []


@pytest.mark.parametrize("operation", ["rename", "quantity", "delete"])
async def test_concurrent_remote_edit_cannot_be_overwritten_or_deleted(connector, server, operation):
    item = (await connector.get_items(server.grocery.id))[0]
    server.rows[item.uid]["text"] = module.render_label("Anderer Name", "Neue Menge")
    original = server.changes
    first = True
    def changes(target_version=None, nodes=None, labels=None):
        nonlocal first
        result = original(target_version, nodes, labels)
        if first:
            result["nodes"] = deepcopy(list(server.rows.values()))
            first = False
        return result
    connector._client._keep_api.changes = changes
    with pytest.raises(ConnectorError, match="inzwischen geändert"):
        if operation == "delete":
            await connector.delete_item(server.grocery.id, item)
        else:
            await connector.update_item(server.grocery.id, item, **({"name": "Unser Name"} if operation == "rename" else {"description": "Unsere Menge"}))
    assert not any(server.calls)
    assert connector._client is None


async def test_timeout_after_server_applied_write_never_replays_dirty_tree(connector, server):
    item = (await connector.get_items(server.grocery.id))[0]
    original = server.changes
    def changes(target_version=None, nodes=None, labels=None):
        result = original(target_version, nodes, labels)
        if nodes:
            raise requests.Timeout("SYNTHETIC_REMOTE_PAYLOAD")
        return result
    connector._client._keep_api.changes = changes
    with pytest.raises(ConnectorError, match="zu langsam"):
        await connector.update_item(server.grocery.id, item, name="Schon gespeichert")
    assert connector._client is None
    assert next(row for row in await connector.get_items(server.grocery.id) if row.uid == item.uid).name == "Schon gespeichert"
    assert len([call for call in server.calls if call]) == 1


@pytest.mark.parametrize("list_kind", ["text_note", "trash", "deleted", "archived"])
async def test_non_list_deleted_and_trashed_targets_never_become_writable(connector, server, list_kind):
    before = deepcopy(server.rows)
    with pytest.raises(ConnectorError, match="nicht mehr verfügbar"):
        await connector.add_item(getattr(server, list_kind).id, "Unzulässig")
    assert server.rows == before and not any(server.calls)


async def test_foreign_or_missing_item_and_duplicate_ids_fail_closed(connector, server):
    item = (await connector.get_items(server.grocery.id))[0]
    with pytest.raises(ConnectorError, match="anderen Liste"):
        await connector.update_item(server.other.id, item, completed=True)
    with pytest.raises(ConnectorError, match="nicht mehr verfügbar"):
        await connector.delete_item(server.grocery.id, Item("missing", "Milch"))
    await connector.list_lists()
    note = connector._client.get(server.grocery.id)
    original_items = module.GoogleKeepConnector._items
    # Corrupt IDs from a remote schema must not collapse duplicate entries.
    item = note.items[0]
    other = note.items[1]
    other.id = item.id
    with pytest.raises(ConnectorError, match="Listenformat"):
        original_items(note)
    assert not any(server.calls)


async def test_parent_with_subitems_is_protected_and_hierarchy_survives_checkbox(connector, server):
    await connector.list_lists()
    note = connector._client.get(server.grocery.id)
    first, second = note.items
    first.indent(second)
    connector._client.sync()
    parent = next(row for row in await connector.get_items(note.id) if row.uid == first.id)
    assert not parent.deletable
    before = deepcopy(server.rows)
    with pytest.raises(UnsupportedOperation, match="Unterpunkten"):
        await connector.delete_item(note.id, parent)
    assert server.rows == before
    child_before = deepcopy(server.rows[second.id])
    updated = await connector.update_item(note.id, parent, completed=not parent.completed)
    assert updated.completed != parent.completed and not updated.deletable
    assert server.rows[second.id] == child_before


@pytest.mark.parametrize("failure,expected", [
    (gkeepapi.exception.LoginException("SYNTHETIC_SECRET"), AuthenticationRequired),
    (requests.Timeout("SYNTHETIC_SECRET"), ConnectorError),
    (ValueError("SYNTHETIC_SECRET"), ConnectorError),
])
async def test_remote_failures_are_fixed_and_failed_tree_is_discarded(connector, server, failure, expected):
    await connector.list_lists()
    server.failure = failure
    with pytest.raises(expected) as error:
        await connector.list_lists()
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert connector._client is None
    await connector.list_lists()
    assert len(server.auth) == 2


async def test_password_login_is_never_attempted(server):
    connector = module.GoogleKeepConnector({"email": "synthetic@example.invalid", "password": "synthetic"})
    with pytest.raises(AuthenticationRequired):
        await connector.test_connection()
    assert server.auth == []
    await connector.close()


async def test_cancelled_worker_keeps_lock_until_thread_finishes_then_close(connector, server):
    await connector.list_lists()
    entered, release = threading.Event(), threading.Event()
    original = connector._client.sync
    calls = []
    loop_thread = threading.get_ident()
    def blocked(*args, **kwargs):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    connector._client.sync = blocked
    first = asyncio.create_task(connector.list_lists())
    assert await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(connector.list_lists())
    await asyncio.sleep(0.02)
    assert len(calls) == 1 and connector._lock.locked()
    release.set()
    assert await asyncio.wait_for(second, 2)
    assert all(thread_id != loop_thread for thread_id in calls)
    await connector.close()
    assert connector._client is None
    with pytest.raises(ConnectorError, match="geschlossen"):
        await connector.list_lists()


def test_bounded_api_has_timeout_and_never_retries_429_or_401(monkeypatch):
    api = module._BoundedKeepAPI(lambda: None)
    calls = []
    def send(**kwargs):
        calls.append(kwargs)
        return NS(status_code=200, raise_for_status=lambda: None,
                  json=lambda: {"error": {"code": 429, "message": "SYNTHETIC_SECRET"}})
    monkeypatch.setattr(api, "_send", send)
    with pytest.raises(ConnectorError) as error:
        api.send(method="POST", json={"nodes": []})
    assert len(calls) == 1 and calls[0]["timeout"] == (10, 30)
    assert calls[0]["allow_redirects"] is False
    assert "SYNTHETIC_SECRET" not in str(error.value)
    monkeypatch.setattr(api, "_send", lambda **kwargs: NS(status_code=401))
    with pytest.raises(gkeepapi.exception.LoginException):
        api.send(method="POST")


@pytest.mark.parametrize("body", [
    {}, {"toVersion": "v1", "truncated": False},
    {"toVersion": "v1", "truncated": False, "nodes": {}},
    {"toVersion": "v1", "truncated": "false", "nodes": []},
    {"toVersion": "v1", "truncated": False, "nodes": [{"type": "LIST"}]},
    {"toVersion": "v1", "truncated": False, "nodes": [{"id": "item", "parentId": "note", "type": "LIST_ITEM", "text": "Milch"}]},
    {"toVersion": "v1", "truncated": False, "nodes": [{"id": "same"}, {"id": "same"}]},
])
def test_full_sync_malformed_response_is_never_a_confirmed_empty_list(monkeypatch, body):
    api = module._BoundedKeepAPI(lambda: None)
    monkeypatch.setattr(api, "_send", lambda **kwargs: NS(status_code=200, raise_for_status=lambda: None, json=lambda: body))
    with pytest.raises(ConnectorError, match="Listenformat"):
        api.send(method="POST", json={"nodes": []})


@pytest.mark.parametrize("embedded", [False, True])
def test_expired_access_token_refreshes_once_for_reads_without_replaying_writes(monkeypatch, embedded):
    api = module._BoundedKeepAPI(lambda: None)
    refreshes, calls = [], []
    api._auth = NS(refresh=lambda: refreshes.append(True))
    def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return NS(status_code=200 if embedded else 401, raise_for_status=lambda: None,
                      json=lambda: {"error": {"code": 401}})
        return NS(status_code=200, raise_for_status=lambda: None,
                  json=lambda: {"nodes": [], "toVersion": "v1", "truncated": False})
    monkeypatch.setattr(api, "_send", send)
    assert api.send(method="POST", json={"nodes": []})["nodes"] == []
    assert len(calls) == 2 and refreshes == [True]
    calls.clear()
    refreshes.clear()
    with pytest.raises(gkeepapi.exception.LoginException):
        api.send(method="POST", json={"nodes": [{"id": "item"}]})
    assert len(calls) == 1 and refreshes == []


def test_bounded_auth_uses_gpsoauth_builders_timeout_and_no_password(monkeypatch):
    calls = []
    class Session:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def mount(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return NS(status_code=200, text="Auth=synthetic-access-token\n", raise_for_status=lambda: None)
    monkeypatch.setattr(module.requests, "Session", Session)
    auth = module._BoundedAuth(gkeepapi.Keep.OAUTH_SCOPES, lambda: None)
    auth.load("synthetic@example.invalid", "synthetic-master", "synthetic-device")
    assert auth.getAuthToken() == "synthetic-access-token"
    assert len(calls) == 1 and calls[0][1]["timeout"] == (10, 30)
    assert calls[0][1]["data"]["EncryptedPasswd"] == "synthetic-master"
    assert calls[0][1]["verify"] is True
    assert calls[0][1]["allow_redirects"] is False
    assert "Passwd" not in calls[0][1]["data"]


def test_real_sdk_constructor_finishes_all_pages_with_bounded_api_and_no_note_cache(monkeypatch):
    server = Server()
    rows = list(server.rows.values())
    midpoint = len(rows) // 2
    calls = []
    def refresh(auth):
        auth._auth_token = "synthetic-access"
        return auth._auth_token
    def send(api, **kwargs):
        calls.append(kwargs)
        page = len(calls)
        body = {"nodes": deepcopy(rows[:midpoint] if page == 1 else rows[midpoint:]),
                "toVersion": f"v{page}", "truncated": page == 1}
        return NS(status_code=200, raise_for_status=lambda: None, json=lambda: body)
    monkeypatch.setattr(module._BoundedAuth, "refresh", refresh)
    monkeypatch.setattr(module._BoundedKeepAPI, "_send", send)
    client = module._new_client("synthetic@example.invalid", "synthetic-master", lambda: None)
    try:
        assert isinstance(client._keep_api, module._BoundedKeepAPI)
        assert client._keep_api._session.trust_env is False
        assert client.get(server.other.id).title == "Einkauf"
        assert client.get(server.text_note.id).text == "Unveränderte fremde Notiz"
        assert len(calls) == 2 and all(call["json"]["nodes"] == [] for call in calls)
        assert "targetVersion" not in calls[0]["json"] and calls[1]["json"]["targetVersion"] == "v1"
        assert client._keep_api._auth.getMasterToken() == "synthetic-master"
    finally:
        module._close_client(client)


def test_expired_operation_aborts_before_request_and_upstream_logs_disabled(caplog):
    connector = module.GoogleKeepConnector({})
    connector._deadline = time.monotonic() - 1
    api = module._BoundedKeepAPI(connector._check_deadline)
    with pytest.raises(TimeoutError):
        api.send(method="POST")
    assert all(logging.getLogger(name).disabled for name in ("gkeepapi", "gkeepapi.node", "gpsoauth"))
    with caplog.at_level(logging.DEBUG):
        assert logging.getLogger("gkeepapi.any_future_child").parent is logging.getLogger("gkeepapi")
        assert logging.getLogger("gkeepapi").propagate is False
        assert logging.getLogger("gkeepapi.any_future_child").handlers == []
        assert all(isinstance(h, logging.NullHandler) for h in logging.getLogger("gkeepapi").handlers)
        logging.getLogger("gkeepapi.any_future_child").critical("SYNTHETIC_PRIVATE_PAYLOAD")
        logging.getLogger("gpsoauth.any_future_child").critical("SYNTHETIC_PRIVATE_PAYLOAD")
    assert "SYNTHETIC_PRIVATE_PAYLOAD" not in caplog.text
