"""Provider metadata and optional accounts through the authenticated API."""

import json
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from listenbruecke.main import PROVIDERS as PROVIDER_NAMES
from listenbruecke.models import ConnectorError
from listenbruecke.providers import PROVIDERS
from listenbruecke.sync import SyncEngine

from test_api import FakeConnector, configure, instance, setup


def test_public_metadata_is_authenticated_complete_and_has_no_vendor_imports(instance):
    _, client, _ = instance
    assert client.get("/api/providers").status_code == 401
    setup(client)
    modules_before = set(sys.modules)
    descriptors = client.get("/api/providers").json()["providers"]
    assert [value["id"] for value in descriptors] == ["monsieur_cuisine", "bring", "alexa", "homeassistant", "google_keep", "cookidoo"]
    assert all(set(value) == {"id", "name", "icon", "className", "description", "auth_kind", "fields", "hints", "docs_url"}
               for value in descriptors)
    google, cookidoo = descriptors[-2:]
    assert {field["name"] for field in google["fields"]} == {"email", "master_token"}
    assert any("alle Keep-Notizen" in hint for hint in google["hints"])
    assert {field["name"]: field.get("default") for field in cookidoo["fields"]}["country"] == "de"
    assert not any(name.startswith(("gkeepapi", "cookidoo_api")) for name in set(sys.modules) - modules_before)
    # A caller modifying the JSON cannot mutate registration metadata.
    google["fields"][0]["label"] = "changed"
    assert PROVIDERS["google_keep"].public()["fields"][0]["label"] != "changed"


@pytest.mark.parametrize("provider,credentials", [
    ("google_keep", {"email": "keep@example.test", "master_token": "SYNTHETIC-MASTER-TOKEN"}),
    ("cookidoo", {"email": "cook@example.test", "password": "SYNTHETIC-PASSWORD"}),
])
def test_optional_provider_can_be_configured_tested_and_selected_without_other_accounts(instance, provider, credentials):
    app, client, _ = instance
    setup(client)
    assert client.put("/api/connections/" + provider, json=credentials).status_code == 200
    response = client.post("/api/connections/" + provider + "/test", json={})
    assert response.status_code == 200 and response.json()["connected"]
    configure(client, "bring")
    targets = [{"provider": name, "list_id": "list"} for name in ("bring", provider)]
    response = client.put("/api/settings", json={"enabled": True, "targets": targets})
    assert response.status_code == 200, response.text
    assert app.state.service.settings()["enabled"]
    status = {item["id"]: item for item in client.get("/api/status").json()["providers"]}
    assert status[provider]["selected"] and not status["alexa"]["selected"]
    assert all(secret not in json.dumps(status) for secret in credentials.values())


def test_unselected_account_changes_preserve_live_ledger_but_future_selection_gets_new_identity(instance):
    app, client, _ = instance
    setup(client)
    store = app.state.service.store
    targets = [{"provider": "bring", "list_id": "list"}, {"provider": "alexa", "list_id": "list"}]
    store.set("sync_settings", {"enabled": True, "targets": targets})
    engine = SyncEngine(store)
    active_key = engine.state_key(targets)
    future_targets = [targets[0], {"provider": "google_keep", "list_id": "list"}]
    old_future_key = engine.state_key(future_targets)
    ledger = {"records": {"synthetic-record": {"creating": {"bring:list": {"before": []}}}}, "ignored": {}}
    store.set(active_key, ledger)
    for method in ("PUT", "DELETE"):
        data = {"email": "keep@example.test", "master_token": "SYNTHETIC-TOKEN"} if method == "PUT" else {}
        assert client.request(method, "/api/connections/google_keep", json=data).status_code == 200
        assert engine.state_key(targets) == active_key
        assert store.get(active_key) == ledger
        assert app.state.service.settings()["enabled"]
        assert store.get("account_generation", 0) == 0
        assert engine.state_key(future_targets) != old_future_key
    assert client.put("/api/connections/bring", json={"email": "other@example.test", "password": "OTHER-ACCOUNT"}).status_code == 200
    assert not app.state.service.settings()["enabled"]
    assert engine.state_key(targets) != active_key


def test_selected_account_is_persistently_paused_before_identity_changes(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    store = app.state.service.store
    old = {"email": "old@example.test", "password": "OLD-SYNTHETIC-PASSWORD"}
    store.set_secret("bring", old)
    store.set("sync_settings", {"enabled": True, "targets": [{"provider": "bring", "list_id": "list"}]})
    real_set = store.set
    def fail_identity(key, value):
        if key == "provider_account_generations":
            raise RuntimeError("Synthetic process interruption")
        return real_set(key, value)
    monkeypatch.setattr(store, "set", fail_identity)
    response = client.put("/api/connections/bring", json={"email": "new@example.test", "password": "NEW-SYNTHETIC-PASSWORD"})
    assert response.status_code == 500
    assert store.get("sync_settings")["enabled"] is False
    assert store.get_secret("bring") == old


def test_optional_providers_can_run_as_the_only_accounts_and_ha_reports_selection(instance):
    app, client, _ = instance
    setup(client)
    for provider, value in [("google_keep", {"email": "keep@example.test", "master_token": "SYNTHETIC-MASTER"}),
                            ("cookidoo", {"email": "cook@example.test", "password": "SYNTHETIC-PASSWORD"})]:
        assert client.put("/api/connections/" + provider, json=value).status_code == 200
        assert client.post("/api/connections/" + provider + "/test", json={}).json()["connected"]
    targets = [{"provider": provider, "list_id": "list"} for provider in ("google_keep", "cookidoo")]
    assert client.put("/api/settings", json={"enabled": True, "targets": targets}).status_code == 200
    token = client.post("/api/integration/token", json={}).json()["token"]
    state = client.get("/api/ha/v1/state", headers={"Authorization": "Bearer " + token}).json()
    assert {provider["id"] for provider in state["providers"] if provider["selected"]} == {"google_keep", "cookidoo"}
    # The native shared list is a sufficient second target for a single account.
    assert client.put("/api/settings", json={"enabled": True, "targets": targets[:1]}).status_code == 200
    assert not app.state.service.credentials("bring")


@pytest.mark.parametrize("provider,bad", [
    ("google_keep", {"email": "keep@example.test", "master_token": "TOKEN", "password": "FORBIDDEN"}),
    ("google_keep", {"email": "keep@example.test", "master_token": ""}),
    ("cookidoo", {"email": "cook@example.test", "password": "PW", "url": "https://unreviewed.test"}),
    ("cookidoo", {"email": "cook@example.test", "password": "PW", "country": "../de", "language": "de-DE"}),
])
def test_provider_input_boundary_rejects_extra_fields_and_wrong_shapes(instance, provider, bad):
    app, client, _ = instance
    setup(client)
    assert client.put("/api/connections/" + provider, json=bad).status_code == 400
    assert app.state.service.store.get_secret(provider) is None


@pytest.mark.parametrize("expires", [True, -1, float("nan"), float("inf"), "123"])
def test_cookidoo_refresh_validation_rejects_unsafe_expiry(expires):
    credentials = {"email": "cook@example.test", "country": "de", "language": "de-DE",
                   "auth_data": {"access_token": "SYNTHETIC-ACCESS", "refresh_token": "SYNTHETIC-REFRESH", "expires_at": expires}}
    with pytest.raises(ConnectorError):
        PROVIDERS["cookidoo"].validate_saved(credentials)


@pytest.mark.parametrize("country,language", [("CH", "en"), ("pl", "pl"), ("cz", "cs"), ("cn", "zh-Hans"), ("de", "de-DE")])
def test_cookidoo_localization_shapes_include_sdk_languages(country, language):
    value = {"email": "cook@example.test", "country": country, "language": language}
    assert PROVIDERS["cookidoo"].validate_input({**value, "password": "SYNTHETIC"})["country"] == country.lower()
    auth = {"access_token": "SYNTHETIC-ACCESS", "refresh_token": "SYNTHETIC-REFRESH", "expires_at": 0}
    assert PROVIDERS["cookidoo"].validate_saved({**value, "auth_data": auth})["language"] == language


def test_cookidoo_success_callback_persists_only_validated_session_and_redacts_nested_tokens(instance, monkeypatch):
    app, client, _ = instance
    setup(client)
    app.state.service.factory = None
    session = {"email": "cook@example.test", "country": "de", "language": "de-DE",
               "auth_data": {"access_token": "SYNTHETIC-ACCESS", "refresh_token": "SYNTHETIC-REFRESH", "expires_at": 0}}
    module = ModuleType("listenbruecke.connectors.cookidoo")
    class StubCookidoo(FakeConnector):
        def __init__(self, credentials, save):
            super().__init__("cookidoo")
            self.save = save
        async def test_connection(self):
            await self.save(session)
            return await super().test_connection()
    module.CookidooConnector = StubCookidoo
    monkeypatch.setitem(sys.modules, module.__name__, module)
    client.put("/api/connections/cookidoo", json={"email": session["email"], "password": "INITIAL-PASSWORD"})
    assert client.post("/api/connections/cookidoo/test", json={}).json()["connected"]
    assert app.state.service.store.get_secret("cookidoo") == session
    stub = app.state.service.clients["cookidoo"]
    with pytest.raises(ConnectorError):
        client.portal.call(stub.save, {**session, "password": "FORBIDDEN"})
    assert app.state.service.store.get_secret("cookidoo") == session
    assert "SYNTHETIC-REFRESH" not in app.state.service.safe_error(ConnectorError("response SYNTHETIC-REFRESH"), provider="cookidoo")


def test_reviewed_registry_extension_does_not_require_service_factory_changes(instance, monkeypatch):
    import listenbruecke.main as main
    app, client, _ = instance
    setup(client)
    # Emulates a reviewed source registration, not arbitrary runtime plugins.
    spec = replace(PROVIDERS["google_keep"], id="community_demo", name="Community Demo",
                   factory=lambda http, credentials, save: FakeConnector("community_demo"))
    monkeypatch.setattr(main, "PROVIDER_REGISTRY", {**PROVIDERS, spec.id: spec})
    monkeypatch.setitem(PROVIDER_NAMES, spec.id, spec.name)
    app.state.service.provider_status[spec.id] = {}
    app.state.service.factory = None
    assert client.put("/api/connections/community_demo", json={"email": "demo@example.test", "master_token": "SYNTHETIC-DEMO"}).status_code == 200
    assert client.post("/api/connections/community_demo/test", json={}).json()["connected"]
    assert client.get("/api/providers").json()["providers"][-1]["id"] == "community_demo"
