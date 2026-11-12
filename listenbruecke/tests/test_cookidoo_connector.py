"""Offline Cookidoo schemas, durable OAuth, and genuine shopping-list CRUD."""

import copy
from dataclasses import asdict, replace
from importlib.metadata import version
import json
import logging
import time

import aiohttp
from cookidoo_api import CookidooAdditionalItem, CookidooAuthData, CookidooConfig, CookidooIngredientItem
from cookidoo_api.exceptions import CookidooAuthException, CookidooParseException, CookidooRequestException
import pytest
from yarl import URL

from listenbruecke.connectors import cookidoo as module
from listenbruecke.connectors.homeassistant import render_label
from listenbruecke.models import AuthenticationRequired, ConnectorError, UnsupportedOperation


def tokens():
    return {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_at": time.time() + 3600}


def credentials():
    return {"email": "person@example.invalid", "country": "de", "language": "de-DE", "auth_data": tokens()}


class FakeSDK:
    def __init__(self, session=None, cfg=None, on_auth_data_update=None, persist=None):
        self.session, self.cfg = session, cfg
        self.callback = on_auth_data_update
        self.auth_data = None
        self.ingredients, self.additional = {}, {}
        self.calls = []
        self.counter = 0
        self.failure = None
        self.after_add_error = False
        self.ingredient_response = None
        self.additional_response = None

    def apply_auth_data(self, data):
        self.auth_data = data

    async def login(self):
        self.calls.append(("login", self.cfg.password))
        self.auth_data = CookidooAuthData(**tokens())
        self.callback(self.auth_data)

    async def get_ingredient_items(self):
        self.calls.append(("read-ingredients",))
        if self.failure:
            raise self.failure("synthetic-provider-secret")
        return copy.deepcopy(self.ingredient_response if self.ingredient_response is not None else list(self.ingredients.values()))

    async def get_additional_items(self):
        self.calls.append(("read-additional",))
        return copy.deepcopy(self.additional_response if self.additional_response is not None else list(self.additional.values()))

    async def add_additional_items(self, labels):
        self.calls.append(("add", list(labels)))
        self.counter += 1
        row = CookidooAdditionalItem("created-" + str(self.counter), labels[0], False)
        self.additional[row.id] = row
        if self.after_add_error:
            self.after_add_error = False
            raise CookidooRequestException("synthetic-provider-secret")
        return [row]

    async def edit_additional_items(self, rows):
        self.calls.append(("rename", rows))
        for row in rows:
            self.additional[row.id] = replace(self.additional[row.id], name=row.name)
        return list(rows)

    async def edit_additional_items_ownership(self, rows):
        self.calls.append(("check-additional", rows))
        for row in rows:
            self.additional[row.id] = replace(self.additional[row.id], is_owned=row.is_owned)
        return list(rows)

    async def edit_ingredient_items_ownership(self, rows):
        self.calls.append(("check-ingredient", rows))
        for row in rows:
            self.ingredients[row.id] = replace(self.ingredients[row.id], is_owned=row.is_owned)
        return list(rows)

    async def remove_additional_items(self, ids):
        self.calls.append(("delete", list(ids)))
        for uid in ids:
            self.additional.pop(uid, None)


def connector():
    saved = []
    async def save(value):
        saved.append(copy.deepcopy(value))
    data = credentials()
    instance = module.CookidooConnector(data, save)
    api = FakeSDK()
    api.auth_data = CookidooAuthData(**data["auth_data"])
    instance._api = api
    return instance, api, saved


@pytest.mark.asyncio
async def test_full_recipe_and_additional_list_preserves_ids_occurrences_case_and_quantity():
    client, api, _ = connector()
    api.ingredients["same-id"] = CookidooIngredientItem("same-id", "ÖL", False, "½ - 1 EL")
    api.additional["same-id"] = CookidooAdditionalItem("same-id", render_label("USB-C ⟦Test⟧", "2 STÜCK \\ Vorrat"), True)
    for number in range(151):
        api.additional[str(number)] = CookidooAdditionalItem(str(number), "Milch", False)
    rows = await client.get_items("shopping")
    assert len(rows) == 153
    assert rows[0].uid == "i:same-id" and rows[1].uid == "a:same-id"
    assert rows[0].description == "½ - 1 EL" and not rows[0].editable and not rows[0].deletable
    assert rows[1].name == "USB-C ⟦Test⟧" and rows[1].description == "2 STÜCK \\ Vorrat" and rows[1].completed
    assert len([row for row in rows if row.name == "Milch"]) == 151


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}, [CookidooIngredientItem("x", "", False, "1 g")],
                                 [CookidooIngredientItem("x", "A", 1, "1 g")],
                                 [CookidooIngredientItem("x", "A", False, None)],
                                 [CookidooIngredientItem("x", "A", False, ""), CookidooIngredientItem("x", "A", False, "")]])
async def test_invalid_or_duplicate_items_never_become_an_empty_success(bad):
    client, api, _ = connector()
    async def malformed():
        return bad
    api.get_ingredient_items = malformed
    with pytest.raises(ConnectorError):
        await client.get_items("shopping")
    assert not any(call[0] in ("add", "rename", "delete") for call in api.calls)


@pytest.mark.asyncio
async def test_recipe_ingredients_only_allow_checkbox_updates_and_preserve_quantity():
    client, api, _ = connector()
    api.ingredients["ingredient"] = CookidooIngredientItem("ingredient", "Salz", False, "½ TL")
    current = (await client.get_items("shopping"))[0]
    with pytest.raises(UnsupportedOperation):
        await client.update_item("shopping", current, description="1 TL")
    with pytest.raises(UnsupportedOperation):
        await client.update_item("shopping", current, name="Pfeffer")
    with pytest.raises(UnsupportedOperation):
        await client.delete_item("shopping", current)
    changed = await client.update_item("shopping", current, completed=True)
    assert changed.name == "Salz" and changed.description == "½ TL" and changed.completed
    reopened = await client.update_item("shopping", changed, completed=False)
    assert not reopened.completed
    assert [call[0] for call in api.calls if call[0].startswith("check-")] == ["check-ingredient", "check-ingredient"]


@pytest.mark.asyncio
async def test_free_item_create_rename_quantity_checkbox_delete_and_existing_duplicates():
    client, api, _ = connector()
    api.additional["existing"] = CookidooAdditionalItem("existing", "Milch", False)
    added = await client.add_item("shopping", "Milch")
    assert added.uid == "a:created-1" and len(api.additional) == 2
    changed = await client.update_item("shopping", added, name="HaferMILCH", description="1½ L", completed=True)
    assert changed.uid == added.uid and changed.fields() == {"name":"HaferMILCH", "description":"1½ L", "completed":True}
    assert api.additional["existing"].name == "Milch"
    await client.delete_item("shopping", changed)
    assert list(api.additional) == ["existing"]
    complete = await client.add_item("shopping", "ÖL", "2 EL", completed=True)
    assert complete.completed and complete.description == "2 EL"


@pytest.mark.asyncio
async def test_remote_concurrent_changes_are_not_overwritten():
    client, api, _ = connector()
    api.additional["a"] = CookidooAdditionalItem("a", "Milch", False)
    current = (await client.get_items("shopping"))[0]
    api.additional["a"].name = "Unabhängige Änderung"
    with pytest.raises(ConnectorError):
        await client.update_item("shopping", current, name="Überschreiben")
    with pytest.raises(ConnectorError):
        await client.delete_item("shopping", current)
    assert not any(call[0] in ("rename", "delete") for call in api.calls)


@pytest.mark.asyncio
async def test_uncertain_create_adopts_one_uid_without_blind_retry_and_engine_read_recovers():
    client, api, _ = connector()
    api.after_add_error = True
    with pytest.raises(ConnectorError):
        await client.add_item("shopping", "Milch", "1 L")
    recovered = await client.add_item("shopping", "Milch", "1 L")
    assert recovered.uid == "a:created-1"
    assert sum(call[0] == "add" for call in api.calls) == 1
    api.after_add_error = True
    with pytest.raises(ConnectorError):
        await client.add_item("shopping", "Brot")
    assert len(await client.get_items("shopping")) == 2
    await client.add_item("shopping", "Eier")
    assert sum(call[0] == "add" for call in api.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_unknown_create_without_unique_candidate_never_reposts(count):
    client, api, _ = connector()
    api.after_add_error = True
    with pytest.raises(ConnectorError):
        await client.add_item("shopping", "Milch")
    api.additional = {str(n):CookidooAdditionalItem(str(n), "Milch", False) for n in range(count)}
    with pytest.raises(ConnectorError):
        await client.add_item("shopping", "Milch")
    assert sum(call[0] == "add" for call in api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CookidooAuthException, CookidooParseException, CookidooRequestException, RuntimeError])
async def test_provider_failures_are_fixed_and_rotated_credentials_are_still_persisted(error):
    client, api, saved = connector()
    api.auth_data = replace(api.auth_data, access_token="synthetic-rotated-access", refresh_token="synthetic-rotated-refresh")
    api.failure = error
    with pytest.raises(AuthenticationRequired if error is CookidooAuthException else ConnectorError) as caught:
        await client.get_items("shopping")
    assert "synthetic-" not in str(caught.value)
    assert saved[-1]["auth_data"]["refresh_token"] == "synthetic-rotated-refresh"
    assert "password" not in saved[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("country,language", [("de","de-DE"),("at","de-AT"),("ch","fr-CH"),("gb","en-GB"),("us","en-US")])
async def test_real_sdk_regions_and_initial_login_save_tokens_without_password_or_files(monkeypatch, tmp_path, country, language):
    made, saved = [], []
    def make(*args, **kwargs):
        api = FakeSDK(*args, **kwargs)
        made.append(api)
        return api
    async def save(data):
        saved.append(data)
    monkeypatch.setattr(module, "_DurableCookidoo", make)
    monkeypatch.chdir(tmp_path)
    client = module.CookidooConnector({"email":"person@example.invalid", "password":"synthetic-password", "country":country.upper(), "language":language}, save)
    try:
        assert (await client.test_connection())[0].id == "shopping"
        assert made[0].cfg.localization.country_code == country
        assert made[0].calls[0] == ("login", "synthetic-password")
        assert made[0].cfg.password == ""
        assert saved and set(saved[-1]) == {"email","country","language","auth_data"}
        assert set(saved[-1]["auth_data"]) == {"access_token","refresh_token","expires_at"}
        assert list(tmp_path.iterdir()) == []
    finally:
        await client.close()
    assert made[0].session.closed


@pytest.mark.asyncio
async def test_restored_tokens_skip_password_login_and_invalid_regions_or_tokens_are_rejected(monkeypatch):
    made = []
    def make(*args, **kwargs):
        api = FakeSDK(*args, **kwargs)
        made.append(api)
        return api
    async def save(_):
        pass
    monkeypatch.setattr(module, "_DurableCookidoo", make)
    client = module.CookidooConnector(credentials(),save)
    try:
        await client.test_connection()
        assert made[0].cfg.password == "" and not any(call[0] == "login" for call in made[0].calls)
    finally:
        await client.close()
    for update in ({"country":"xx"}, {"auth_data":{**tokens(),"expires_at":True}}, {"auth_data":{**tokens(),"expires_at":float('nan')}}):
        bad = module.CookidooConnector({**credentials(),**update},save)
        try:
            with pytest.raises(ConnectorError):
                await bad.get_items("shopping")
        finally:
            await bad.close()


def test_sdk_logs_do_not_escape_but_app_diagnostics_remain(caplog):
    connector()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("cookidoo_api.cookidoo").critical("synthetic-access-must-not-appear")
        logging.getLogger("listenbruecke.test").warning("Application diagnostics remain available")
    assert "synthetic-access" not in caplog.text
    assert "Application diagnostics" in caplog.text


class Response:
    def __init__(self, payload):
        self.payload, self.status = payload, 200
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        pass
    async def json(self):
        return copy.deepcopy(self.payload)
    async def text(self):
        return json.dumps(self.payload)
    def raise_for_status(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_fails", [False,True])
async def test_real_pinned_sdk_flushes_transparent_refresh_before_mutation(storage_fails):
    assert version("cookidoo-api") == "0.19.0"
    persisted, writes = [], []
    class Session:
        def post(self,url,**kwargs):
            assert kwargs["data"]["grant_type"] == "refresh_token"
            return Response({"access_token":"synthetic-new-access", "refresh_token":"synthetic-new-refresh", "expires_in":3600})
        def request(self,method,url,**kwargs):
            assert persisted[-1]["refresh_token"] == "synthetic-new-refresh"
            writes.append((method,url))
            return Response({"data":[]})
    async def persist():
        if storage_fails:
            raise ConnectorError("Die erneuerte Cookidoo-Anmeldung konnte nicht gespeichert werden.")
        persisted.append(asdict(api.auth_data))
    api = module._DurableCookidoo(Session(),cfg=CookidooConfig(),persist=persist)
    api._oidc = {"token_endpoint":"https://ciam.prod.cookidoo.vorwerk-digital.com/token-srv/token"}
    api.apply_auth_data(CookidooAuthData("synthetic-old","synthetic-refresh",0.0))
    operation = api._request_json("post",URL("https://cookidoo.de/shopping/de-DE/additional-items/add"),"add additional items",json={"itemsValue":["Milch"]})
    if storage_fails:
        with pytest.raises(ConnectorError):
            await operation
        assert not writes
    else:
        assert await operation == {"data":[]}
        assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"recipes":[],"additionalItems":[]},
                                   {"recipes":[],"customerRecipes":[],"additionalItems":[],"nextToken":"more"},
                                   {"recipes":[],"customerRecipes":[],"additionalItems":[],"_links":{"next":{"href":"page2"}}}])
async def test_real_sdk_adapter_rejects_future_paging_and_missing_payload_sections(body):
    class Session:
        def request(self,*args,**kwargs):
            return Response(body)
    async def persist():
        pass
    api = module._DurableCookidoo(Session(),persist=persist)
    api.apply_auth_data(CookidooAuthData(**tokens()))
    with pytest.raises(ConnectorError):
        await api._request_json("get",URL("https://cookidoo.de/shopping/de-DE"),"loading ingredient items")


@pytest.mark.asyncio
async def test_real_pinned_sdk_parses_standard_custom_recipes_and_additional_items_end_to_end():
    payload = {"recipes":[{"recipeIngredientGroups":[{
        "id":"ingredient", "ingredientNotation":"ÖL", "isOwned":False,
        "quantity":{"value":1.5}, "unitNotation":"EL", "preparation":", kalt"}]}],
        "customerRecipes":[{"recipeIngredientGroups":[{
            "id":"custom-ingredient", "ingredientNotation":"Mehl", "isOwned":True,
            "quantity":{"from":2,"to":4}, "unitNotation":"g"}]}],
        "additionalItems":[{"id":"ingredient", "name":render_label("USB-C","2 STÜCK"), "isOwned":False}]}
    calls = []
    class Session:
        def request(self,method,url,**kwargs):
            calls.append((method,str(url)))
            return Response(payload)
    client, _, _ = connector()
    api = module._DurableCookidoo(Session(),persist=client._persist,on_auth_data_update=client._capture_auth)
    api.apply_auth_data(CookidooAuthData(**client.credentials["auth_data"]))
    api._endpoints_resolved = True
    api._endpoint_overrides = {"pantry:home":"shopping/{language}"}
    client._api = api
    rows = await client.get_items("shopping")
    assert [(item.uid,item.name,item.description,item.completed) for item in rows] == [
        ("i:ingredient","ÖL","1.5 EL",False), ("i:custom-ingredient","Mehl","2 - 4 g",True),
        ("a:ingredient","USB-C","2 STÜCK",False)]
    assert calls == [("get","https://cookidoo.ch/shopping/de-CH")]*2


@pytest.mark.asyncio
async def test_unconfirmed_provider_mutations_are_errors_and_missing_delete_is_idempotent():
    client, api, _ = connector()
    api.additional["a"] = CookidooAdditionalItem("a", "Milch", False)
    current = (await client.get_items("shopping"))[0]
    async def no_op(rows):
        return rows
    api.edit_additional_items = no_op
    with pytest.raises(ConnectorError,match="bestätigt"):
        await client.update_item("shopping",current,name="Brot")
    async def no_delete(ids):
        pass
    api.remove_additional_items = no_delete
    with pytest.raises(ConnectorError,match="bestätigt"):
        await client.delete_item("shopping",current)
    api.additional.clear()
    await client.delete_item("shopping",current)
