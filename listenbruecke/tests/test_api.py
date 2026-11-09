"""Authenticated HTTP workflow tests with temporary storage and fake providers."""

import asyncio
import io
import json
import sys
import time
import zipfile
from dataclasses import replace
from types import ModuleType
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from listenbruecke.main import COOKIE, create_app
from listenbruecke.models import ConnectorError, Item, ShoppingList


class FakeConnector:
    def __init__(self, name):
        self.name = name
        self.items = {}
        self.closed = False

    async def test_connection(self):
        return [ShoppingList("list", self.name + " Einkauf")]

    async def get_items(self, list_id):
        return list(self.items.values())

    async def add_item(self, list_id, **fields):
        item = Item(f"{self.name}-{len(self.items)}", **fields)
        self.items[item.uid] = item
        return item

    async def update_item(self, list_id, item, **fields):
        result = replace(item, **fields)
        self.items[item.uid] = result
        return result

    async def delete_item(self, list_id, item):
        self.items.pop(item.uid, None)

    async def close(self):
        self.closed = True


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    clients = {}
    async def factory(provider, credentials):
        clients[provider] = FakeConnector(provider)
        return clients[provider]
    app = create_app(tmp_path, factory=factory, background=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client, clients


def setup(client):
    response = client.post("/api/setup", json={"password": "correct-test-password"})
    assert response.status_code == 200, response.text
    return response


def configure(client, provider):
    data = {"email": "account@example.test", "password": "PROVIDER-PASSWORD-SECRET"} if provider == "bring" else {"url": "http://homeassistant.local:8123", "token": "PROVIDER-TOKEN-SECRET"}
    response = client.put("/api/connections/" + provider, json=data)
    assert response.status_code == 200, response.text
    response = client.post("/api/connections/" + provider + "/test", json={})
    assert response.status_code == 200
    assert response.json()["connected"]
    return data


def test_setup_login_logout_and_session_guards(instance):
    app, client, _ = instance
    assert client.get("/api/session").json()["setup_required"]
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/setup", json={"password": "too-short"}).status_code == 400
    response = setup(client)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/setup", json={"password": "another-password"}).status_code == 409
    token = client.cookies.get(COOKIE)
    assert client.post("/api/logout", json={}).status_code == 200
    assert not app.state.service.store.valid_session(token)
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct-test-password"}).status_code == 200


def test_all_sensitive_api_routes_require_authentication(instance):
    _, client, _ = instance
    for method, url in [("GET", "/api/status"), ("GET", "/api/events"), ("GET", "/api/settings"),
                        ("GET", "/api/browser-helper.zip"), ("POST", "/api/sync"),
                        ("PUT", "/api/connections/bring"), ("DELETE", "/api/connections/bring"),
                        ("POST", "/api/connections/monsieur_cuisine/auth/start"),
                        ("POST", "/api/connections/alexa/auth/start"),
                        ("GET", "/api/connections/alexa/auth/status")]:
        response = client.request(method, url, json={} if method != "GET" else None)
        assert response.status_code == 401, (method, url, response.text)


def test_origin_host_json_size_and_input_redaction(instance):
    _, client, _ = instance
    assert client.post("/api/setup", json={}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/setup", json={}, headers={"host": "evil.example", "origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/setup", json={}, headers={"origin": "http://[invalid"}).status_code == 403
    assert client.post("/api/setup", content="password=INPUT-SECRET", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
    response = client.post("/api/setup", json=["INPUT-SECRET"])
    assert response.status_code == 422
    assert "INPUT-SECRET" not in response.text
    response = client.post("/api/setup", json={"password": "INPUT-SECRET" * 4000})
    assert response.status_code == 413
    assert "INPUT-SECRET" not in response.text
    setup(client)
    response = client.put("/api/connections/homeassistant", json={"url": "http://user:INPUT-SECRET@ha.local", "token": "TOKEN-SECRET"})
    assert response.status_code == 400
    assert "SECRET" not in response.text
    assert client.put("/api/connections/homeassistant", json={"url": "http://ha.local:invalid", "token": "TOKEN-SECRET"}).status_code == 400


def test_save_test_and_status_never_return_stored_credentials(instance):
    app, client, clients = instance
    setup(client)
    credentials = configure(client, "bring")
    assert app.state.service.store.get_secret("bring") == credentials
    response = client.get("/api/status")
    assert "PROVIDER-PASSWORD-SECRET" not in response.text
    assert "account@example.test" not in response.text
    assert client.get("/api/events").json()[0]["message"] == "Bring!: Verbindung erfolgreich geprüft."
    raw = app.state.service.store._db.execute("SELECT value FROM secrets WHERE key='bring'").fetchone()[0]
    assert b"PROVIDER-PASSWORD-SECRET" not in raw
    for error in (RuntimeError, ConnectorError):
        async def broken_factory(provider, credentials):
            raise error("PROVIDER-PASSWORD-SECRET")
        app.state.service.factory = broken_factory
        assert client.request("DELETE", "/api/connections/bring", json={}).status_code == 200
        client.put("/api/connections/bring", json=credentials)
        response = client.post("/api/connections/bring/test", json={})
        assert not response.json()["connected"]
        assert "SECRET" not in response.text
        assert "SECRET" not in client.get("/api/status").text
        assert "SECRET" not in client.get("/api/events").text


def test_settings_and_manual_sync_preserve_running_configuration(instance):
    app, client, clients = instance
    setup(client)
    assert client.put("/api/settings", json={"enabled": True}).status_code == 400
    assert client.put("/api/settings", json={"enabled": False}).status_code == 200
    for provider in ("bring", "homeassistant"):
        configure(client, provider)
    targets = [{"provider": provider, "list_id": "list"} for provider in ("bring", "homeassistant")]
    settings = {"enabled": True, "interval_seconds": 120, "targets": targets, "propagate_deletions": False}
    assert client.put("/api/settings", json={**settings, "targets": targets[:1]}).status_code == 400
    assert client.put("/api/settings", json={**settings, "interval_seconds": True}).status_code == 400
    assert client.put("/api/settings", json={**settings, "targets": [{"provider": [], "list_id": "list"}]}).status_code == 400
    assert client.put("/api/settings", json=settings).status_code == 200
    clients["bring"].items["milk"] = Item("milk", "Milch", "1 l")
    response = client.post("/api/sync", json={})
    assert response.status_code == 202, response.text
    assert response.json()["accepted"]
    async def completed():
        await app.state.service.manual_task
    client.portal.call(completed)
    assert client.get("/api/settings").json() == settings
    assert client.get("/api/status").json()["sync"]["items_synced"] == 1
    assert app.state.service.store.get("sync_settings") == settings


def test_manual_sync_is_async_blocks_duplicates_and_reports_progress(instance):
    app, client, clients = instance
    setup(client)
    for provider in ("bring", "homeassistant"):
        configure(client, provider)
    targets = [{"provider": provider, "list_id": "list"} for provider in ("bring", "homeassistant")]
    client.put("/api/settings", json={"enabled": True, "targets": targets})
    gate = asyncio.Event()
    original = clients["bring"].get_items
    async def blocked(list_id):
        await gate.wait()
        return await original(list_id)
    clients["bring"].get_items = blocked
    response = client.post("/api/sync", json={})
    assert response.status_code == 202
    assert client.get("/api/status").json()["sync"]["running"]
    assert client.post("/api/sync", json={}).status_code == 400
    client.portal.call(gate.set)
    async def completed():
        await app.state.service.manual_task
    client.portal.call(completed)
    status = client.get("/api/status").json()["sync"]
    assert not status["running"]
    assert status["last_success"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_sync_refreshes_restart_connection_flags_only_after_all_sources_succeed(instance, unavailable):
    app, client, clients = instance
    setup(client)
    for provider in ("bring", "homeassistant"):
        configure(client, provider)
    targets = [{"provider": provider, "list_id": "list"} for provider in ("bring", "homeassistant")]
    client.put("/api/settings", json={"enabled": True, "targets": targets})
    cached_lists = {}
    for provider in clients:
        status = app.state.service.provider_status[provider]
        cached_lists[provider] = status["lists"]
        status.update(connected=False, error="Vorheriger Fehler", last_test="2000-01-01T00:00:00")
        app.state.service.store.set("provider_status:" + provider, status)
    if unavailable:
        async def broken_read(list_id):
            raise ConnectorError("Die Quelle ist nicht erreichbar.")
        clients["homeassistant"].get_items = broken_read
    assert client.post("/api/sync", json={}).status_code == 202
    async def completed():
        await app.state.service.manual_task
    client.portal.call(completed)
    providers = {provider["id"]: provider for provider in client.get("/api/status").json()["providers"]}
    for provider in clients:
        assert providers[provider]["connected"] is (not unavailable)
        assert providers[provider]["lists"] == cached_lists[provider]
        if not unavailable:
            assert providers[provider]["error"] is None
            assert providers[provider]["last_test"] != "2000-01-01T00:00:00"
            assert app.state.service.store.get("provider_status:" + provider)["connected"]


def test_oauth_unknown_expired_and_replayed_state_never_writes_credentials(instance):
    app, client, _ = instance
    setup(client)
    assert client.post("/api/connections/monsieur_cuisine/auth/complete", json={"code": "CODE-SECRET", "state": "unknown"}).status_code == 400
    start = client.post("/api/connections/monsieur_cuisine/auth/start", json={}).json()
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    assert "verifier" not in start
    pending = app.state.service.store.get_secret("oauth:" + state)
    pending["expires"] = time.time() - 1
    app.state.service.store.set_secret("oauth:" + state, pending)
    for _ in range(2):
        response = client.post("/api/connections/monsieur_cuisine/auth/complete", json={"code": "CODE-SECRET", "state": state})
        assert response.status_code == 400
        assert "CODE-SECRET" not in response.text
    assert app.state.service.store.get_secret("monsieur_cuisine") is None


def test_oauth_success_is_consumed_once_and_credentials_are_not_returned(instance):
    app, client, _ = instance
    setup(client)
    start = client.post("/api/connections/monsieur_cuisine/auth/start", json={}).json()
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    calls = []
    async def complete(auth, code, verifier):
        calls.append(code)
        result = {"jwt": "JWT-SECRET", "lidl_refresh_token": "REFRESH-SECRET"}
        await auth._save_credentials(result)
        return result
    with patch("listenbruecke.connectors.mc_auth.MCAuth.complete_login", complete):
        payload = {"code": "CODE-SECRET", "state": state}
        response = client.post("/api/connections/monsieur_cuisine/auth/complete", json=payload)
        assert response.status_code == 200
        assert "SECRET" not in response.text
        assert client.post("/api/connections/monsieur_cuisine/auth/complete", json=payload).status_code == 400
    assert calls == ["CODE-SECRET"]
    assert app.state.service.store.get_secret("monsieur_cuisine")["jwt"] == "JWT-SECRET"


def test_browser_helper_archive_includes_manifest_and_license(instance):
    _, client, _ = instance
    setup(client)
    response = client.get("/api/browser-helper.zip")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert {"manifest.json", "background.js", "handoff.html", "THIRD_PARTY_LICENSES.md"} <= set(archive.namelist())
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["host_permissions"] == ["https://accounts.lidl.com/*"]


def test_ingress_trust_requires_supervisor_peer_and_header(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "SUPERVISOR-SECRET")
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    headers = {"x-ingress-path": "/api/hassio_ingress/session-id"}
    app = create_app(tmp_path, background=False)
    with TestClient(app, client=("172.30.32.2", 1234), headers=headers) as client:
        response = client.get("/api/session")
        assert response.json() == {"authenticated": True, "setup_required": False, "ingress": True}
        response = client.get("/api/status")
        assert response.status_code == 200
        assert "SUPERVISOR-SECRET" not in response.text
    with TestClient(create_app(tmp_path, background=False), client=("192.168.1.7", 1234), headers=headers) as client:
        assert client.get("/api/status").status_code == 401


def test_shutdown_closes_factory_connectors_and_http(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    connector = FakeConnector("bring")
    async def factory(provider, credentials):
        return connector
    app = create_app(tmp_path, factory=factory, background=False)
    with TestClient(app) as client:
        setup(client)
        configure(client, "bring")
        http = app.state.service.http
        assert not http.is_closed
    assert connector.closed
    assert http.is_closed


def test_shutdown_cancels_and_joins_active_manual_sync(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    connectors = {}
    gate = asyncio.Event()
    async def factory(provider, credentials):
        connector = FakeConnector(provider)
        async def blocked(list_id):
            await gate.wait()
            return []
        connector.get_items = blocked
        connectors[provider] = connector
        return connector
    app = create_app(tmp_path, factory=factory, background=False)
    with TestClient(app) as client:
        setup(client)
        for provider in ("bring", "homeassistant"):
            configure(client, provider)
        client.put("/api/settings", json={"targets": [{"provider": provider, "list_id": "list"} for provider in connectors]})
        assert client.post("/api/sync", json={}).status_code == 202
        assert client.get("/api/status").json()["sync"]["running"]
        task = app.state.service.manual_task
    assert task.done()
    assert task.cancelled()
    assert all(connector.closed for connector in connectors.values())
    assert app.state.service.http.is_closed


def test_busy_sync_rejects_configuration_promptly_without_consuming_login(instance):
    app, client, clients = instance
    setup(client)
    for provider in ("bring", "homeassistant"):
        configure(client, provider)
    targets = [{"provider": provider, "list_id": "list"} for provider in clients]
    client.put("/api/settings", json={"targets": targets})
    start = client.post("/api/connections/monsieur_cuisine/auth/start", json={}).json()
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    gate = asyncio.Event()
    async def blocked(list_id):
        await gate.wait()
        return []
    clients["bring"].get_items = blocked
    assert client.post("/api/sync", json={}).status_code == 202
    assert client.get("/api/status").json()["sync"]["running"]
    for method, path, body in [
        ("DELETE", "/api/connections/bring", {}),
        ("POST", "/api/connections/bring/test", {}),
        ("PUT", "/api/settings", {"targets": targets}),
        ("POST", "/api/connections/monsieur_cuisine/auth/complete", {"code": "synthetic-code", "state": state}),
    ]:
        response = client.request(method, path, json=body)
        assert response.status_code == 409, response.text
    assert app.state.service.store.get_secret("oauth:" + state) is not None
    assert app.state.service.store.get_secret("bring") is not None
    client.portal.call(gate.set)
    async def finished():
        await app.state.service.manual_task
    client.portal.call(finished)


def stub_alexa(monkeypatch, login):
    module = ModuleType("listenbruecke.connectors.alexa")
    class StubAlexa(FakeConnector):
        def __init__(self, credentials, save):
            super().__init__("alexa")
            self.credentials = credentials
            self.save = save
    StubAlexa.login = staticmethod(login)
    module.AlexaConnector = StubAlexa
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return StubAlexa


def finish_alexa_login(app, client):
    async def finished():
        await app.state.service.alexa_auth_task
    client.portal.call(finished)
    return client.get("/api/connections/alexa/auth/status").json()


def test_alexa_otp_login_commits_only_encrypted_session_and_explicit_connector(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    submitted = []
    session = {"email": "alexa@example.invalid", "login_data": {"tokens": {"refresh_token": "SYNTHETIC-ALEXA-SESSION"}}}
    async def login(email, password, code):
        submitted.append((email, password, code))
        return session
    stub_alexa(monkeypatch, login)
    app.state.service.factory = None
    response = client.post("/api/connections/alexa/auth/start", json={
        "email": "alexa@example.invalid", "password": "SYNTHETIC-ALEXA-PASSWORD", "code": "123456",
    })
    assert response.status_code == 202 and response.json() == {"running": True}
    assert finish_alexa_login(app, client) == {"running": False, "complete": True, "error": None}
    assert submitted == [("alexa@example.invalid", "SYNTHETIC-ALEXA-PASSWORD", "123456")]
    assert app.state.service.store.get_secret("alexa") == session
    assert app.state.service.provider("alexa")["connected"]
    assert app.state.service.store.get("account_generation") == 1
    assert app.state.service.store.get("sync_settings")["enabled"] is False
    output = client.get("/api/status").text + client.get("/api/events").text
    assert all(secret not in output for secret in ("SYNTHETIC-ALEXA-PASSWORD", "SYNTHETIC-ALEXA-SESSION", "123456"))
    encrypted = app.state.service.store._db.execute("SELECT value FROM secrets WHERE key='alexa'").fetchone()[0]
    assert b"SYNTHETIC-ALEXA-SESSION" not in encrypted
    saved = {"email": session["email"], "login_data": {"tokens": {"refresh_token": "SYNTHETIC-REFRESHED-SESSION"}}}
    client.portal.call(app.state.service.clients["alexa"].save, saved)
    assert app.state.service.store.get_secret("alexa") == saved


@pytest.mark.parametrize("result", [None, {"email": "test@example.invalid", "login_data": {"tokens": {}}, "password": "FORBIDDEN"},
                                    {"email": "test@example.invalid", "login_data": {"password": "FORBIDDEN"}}])
def test_alexa_failed_or_unsafe_login_preserves_existing_account(instance, monkeypatch, result):
    app, client, _ = instance
    setup(client)
    original = {"email": "original@example.invalid", "login_data": {"tokens": {"refresh_token": "ORIGINAL-SESSION"}}}
    app.state.service.store.set_secret("alexa", original)
    app.state.service.store.set("sync_settings", {"enabled": True})
    async def login(**kwargs):
        if result is None:
            raise ConnectorError("SYNTHETIC-PRIVATE-RESPONSE password code session")
        return result
    stub_alexa(monkeypatch, login)
    response = client.post("/api/connections/alexa/auth/start", json={"email": "new@example.invalid", "password": "NEW-PASSWORD", "code": "123456"})
    assert response.status_code == 202
    status = finish_alexa_login(app, client)
    assert not status["running"] and not status["complete"] and status["error"]
    assert "SYNTHETIC-PRIVATE-RESPONSE" not in json.dumps(status)
    assert app.state.service.store.get_secret("alexa") == original
    assert app.state.service.store.get("sync_settings") == {"enabled": True}
    assert app.state.service.store.get("account_generation", 0) == 0


def test_alexa_login_locks_configuration_sync_and_import_until_done(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    gate = client.portal.call(asyncio.Event)
    async def login(**kwargs):
        await gate.wait()
        return {"email": kwargs["email"], "login_data": {"tokens": {"refresh_token": "SYNTHETIC-SESSION"}}}
    stub_alexa(monkeypatch, login)
    payload = {"email": "test@example.invalid", "password": "test-password", "code": "123456"}
    assert client.post("/api/connections/alexa/auth/start", json=payload).status_code == 202
    assert client.get("/api/connections/alexa/auth/status").json()["running"]
    for method, path, body in [
        ("POST", "/api/connections/alexa/auth/start", payload),
        ("PUT", "/api/connections/bring", {"email": "a@example.invalid", "password": "synthetic"}),
        ("DELETE", "/api/connections/alexa", {}),
        ("POST", "/api/connections/bring/test", {}),
        ("PUT", "/api/settings", {"targets": []}),
        ("POST", "/api/backup/import", {"archive": "eA=="}),
    ]:
        assert client.request(method, path, json=body).status_code == 409, path
    assert client.post("/api/sync", json={}).status_code == 400
    assert app.state.service.store.get_secret("alexa") is None
    client.portal.call(gate.set)
    assert finish_alexa_login(app, client)["complete"]


def test_alexa_login_validation_and_restore_pending_refuse_without_storing_secrets(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    payload = {"email": "test@example.invalid", "password": "INPUT-PRIVATE-PASSWORD", "code": "123456"}
    for invalid in ({**payload, "code": "bad-otp"}, {**payload, "mfa_seed": "FORBIDDEN-SEED"}, {**payload, "password": None}):
        response = client.post("/api/connections/alexa/auth/start", json=invalid)
        assert response.status_code == 400
        assert "INPUT-PRIVATE-PASSWORD" not in response.text and "FORBIDDEN-SEED" not in response.text
    assert client.put("/api/connections/alexa", json=payload).status_code == 400
    app.state.service.restore_pending = True
    assert client.post("/api/connections/alexa/auth/start", json=payload).status_code == 409
    assert app.state.service.alexa_auth_task is None
    assert app.state.service.store.get_secret("alexa") is None


def test_alexa_login_timeout_has_fixed_status_and_preserves_account(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    original = {"email": "original@example.invalid", "login_data": {"refresh_token": "ORIGINAL-SESSION"}}
    app.state.service.store.set_secret("alexa", original)
    async def login(**kwargs):
        await asyncio.Event().wait()
    stub_alexa(monkeypatch, login)
    real_wait_for = asyncio.wait_for
    timeouts = []
    async def expire_login(awaitable, timeout):
        if timeout == 90:
            timeouts.append(timeout)
            awaitable.close()
            raise TimeoutError("UNTRUSTED-TIMEOUT-RESPONSE")
        return await real_wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", expire_login)
    assert client.post("/api/connections/alexa/auth/start", json={"email": "new@example.invalid", "password": "synthetic", "code": "123456"}).status_code == 202
    status = finish_alexa_login(app, client)
    assert timeouts == [90]
    assert not status["running"] and not status["complete"] and status["error"]
    assert "UNTRUSTED-TIMEOUT-RESPONSE" not in json.dumps(status)
    assert app.state.service.store.get_secret("alexa") == original


def test_alexa_connection_errors_are_fixed_and_nested_session_values_are_redacted(instance):
    app, client, _ = instance
    setup(client)
    service = app.state.service
    service.store.set_secret("alexa", {"email": "alexa@example.invalid", "login_data": {"tokens": {"refresh_token": "SYNTHETIC-NESTED-SESSION"}}})
    connector = FakeConnector("alexa")
    async def fail():
        raise ConnectorError("UNTRUSTED-AMAZON-RESPONSE")
    connector.test_connection = fail
    service.clients["alexa"] = connector
    response = client.post("/api/connections/alexa/test", json={})
    assert not response.json()["connected"]
    assert "UNTRUSTED-AMAZON-RESPONSE" not in response.text
    assert "SYNTHETIC-NESTED-SESSION" not in service.safe_error(ConnectorError("SYNTHETIC-NESTED-SESSION failed"))


def test_shutdown_cancels_alexa_login_without_overwriting_account(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    cancelled = []
    async def login(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    stub_alexa(monkeypatch, login)
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        setup(client)
        original = {"email": "original@example.invalid", "login_data": {"refresh_token": "ORIGINAL-SESSION"}}
        app.state.service.store.set_secret("alexa", original)
        response = client.post("/api/connections/alexa/auth/start", json={"email": "new@example.invalid", "password": "synthetic", "code": "123456"})
        assert response.status_code == 202
        service = app.state.service
    assert cancelled and service.alexa_auth_task.cancelled()
    from listenbruecke.store import Store
    with_store = Store(tmp_path)
    try:
        assert with_store.get_secret("alexa") == original
    finally:
        with_store.close()


def test_four_target_configuration_and_conditional_alexa_ha_mirror_guard(instance):
    app, client, clients = instance
    setup(client)
    selected = ("bring", "monsieur_cuisine", "homeassistant", "alexa")
    for provider in selected:
        app.state.service.store.set_secret(provider, {"synthetic": True})
        clients[provider] = FakeConnector(provider)
        app.state.service.clients[provider] = clients[provider]
        app.state.service.provider_status[provider] = {"connected": True, "lists": [{"id": "list", "name": provider}]}
    checks = []
    async def independent(list_id, providers):
        checks.append((list_id, providers))
    clients["homeassistant"].check_independent_source = independent
    targets = [{"provider": provider, "list_id": "list"} for provider in selected]
    assert client.put("/api/settings", json={"enabled": True, "targets": targets}).status_code == 200
    assert checks and checks[0][1] == set(selected)
    clients["bring"].items["milk"] = Item("milk", "Milch", "1 l")
    result = client.portal.call(app.state.service.synchronize)
    assert result["ok"] and all(connector.items for connector in clients.values())
    async def mirror(list_id, providers):
        raise ConnectorError("Diese Home-Assistant-Liste ist bereits dieselbe Alexa-Liste.")
    clients["homeassistant"].check_independent_source = mirror
    assert client.put("/api/settings", json={"enabled": True, "targets": targets}).status_code == 400
    with pytest.raises(ConnectorError, match="Alexa-Liste"):
        client.portal.call(app.state.service.synchronize)
    # A HA Alexa entity stays usable when direct Alexa isn't a selected source.
    ha_only_alexa = [target for target in targets if target["provider"] != "alexa"]
    assert client.put("/api/settings", json={"enabled": True, "targets": ha_only_alexa}).status_code == 200
