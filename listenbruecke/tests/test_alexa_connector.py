"""Offline Alexa CRUD/auth tests, plus compatibility with pinned real SDK."""

import copy
import inspect
import logging
from http.cookies import SimpleCookie
from importlib.metadata import version

import aiohttp
import pytest
from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.exceptions import CannotAuthenticate, CannotConnect, CannotRetrieveData
from yarl import URL

from listenbruecke.connectors import alexa
from listenbruecke.connectors.homeassistant import render_label
from listenbruecke.models import AuthenticationRequired, ConnectorError


def session_data():
    return {"site": "https://www.amazon.de", "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh", "expires": 9999999999,
            "website_cookies": {"session-id": "synthetic-session"},
            "store_authentication_cookie": {"cookie": "synthetic-cookie"},
            "device_info": {"device_serial_number": "synthetic-device"},
            "customer_info": {"home_region": "EU", "user_id": "synthetic-user"},
            "account_customer_id": "synthetic-customer"}


class Response:
    def __init__(self, data, status=200, url=None):
        self.data, self.status = data, status
        self.url = url or URL("https://www.amazon.de/alexashoppinglists/api/v2/lists/fetch")
        self.cookies = SimpleCookie()
        self.headers = {"Content-Type": "application/json"}
        self.history = []

    async def json(self, **kwargs):
        return copy.deepcopy(self.data)

    async def read(self):
        import json
        return json.dumps(self.data).encode()


class Login:
    def __init__(self, data):
        self.data = data
        self.stored_calls = self.cookie_calls = 0

    async def login_mode_stored_data(self):
        self.stored_calls += 1
        return self.data

    async def login_mode_interactive(self, code):
        return self.data

    async def _refresh_auth_cookies(self):
        self.cookie_calls += 1
        self.data["website_cookies"]["csrf"] = "synthetic-rotated-cookie"


class FakeAPI:
    domain = "de"

    def __init__(self, data):
        self.login = Login(data)
        self._http_wrapper = self
        self.rows, self.calls, self.pages = {}, [], None
        self.after_add_error = False
        self.auth_errors = 0
        self.counter = 0

    def add(self, uid, name, completed=False, version=1):
        self.rows[uid] = {"itemId": uid, "itemName": name, "version": version,
                          "itemStatus": "COMPLETE" if completed else "ACTIVE"}

    async def session_request(self, *, method, url, input_data, json_data, fail_fast):
        self.calls.append((method, url, copy.deepcopy(input_data), fail_fast))
        assert fail_fast is True
        assert json_data is True
        if self.auth_errors:
            self.auth_errors -= 1
            raise CannotAuthenticate("synthetic-secret-must-not-leak")
        path = url.path
        if path.endswith("/lists/fetch"):
            return None, Response({"listInfoList": [{"listId": "list", "listType": "SHOP", "listName": None}]})
        if path.endswith("/items/fetch"):
            if self.pages is not None:
                return None, Response(self.pages.pop(0))
            offset = int(input_data.get("nextToken", 0))
            rows = list(self.rows.values())
            token = str(offset + 100) if len(rows) > offset + 100 else None
            return None, Response({"itemInfoList": rows[offset:offset + 100], "nextToken": token})
        if method == "POST" and path.endswith("/items"):
            self.counter += 1
            self.add("created-" + str(self.counter), input_data["items"][0]["itemName"])
            if self.after_add_error:
                self.after_add_error = False
                raise CannotConnect("synthetic-secret-must-not-leak")
        elif method in ("PUT", "DELETE"):
            uid = url.parts[-1]
            current = self.rows[uid]
            if str(current["version"]) != url.query["version"]:
                raise CannotRetrieveData("synthetic-secret-version")
            if method == "DELETE":
                del self.rows[uid]
            else:
                for field in input_data["itemAttributesToUpdate"]:
                    current[field["type"]] = field["value"]
                current["version"] += 1
        else:
            raise AssertionError("Unexpected request")
        return None, Response({})


def connector():
    saved = []
    async def store(credentials):
        saved.append(copy.deepcopy(credentials))
    instance = alexa.AlexaConnector({"email": "user@example.invalid", "login_data": session_data()}, store)
    api = FakeAPI(instance.credentials["login_data"])
    instance._api = api
    return instance, api, saved


@pytest.mark.asyncio
async def test_full_pagination_preserves_exact_case_and_quantity():
    client, api, _ = connector()
    name, quantity = "USB-C OlivenÖL ⟦Test⟧", "½ L bei 20°C \\ Vorrat"
    api.add("special", render_label(name, quantity))
    for number in range(150):
        api.add("id-" + str(number), "Artikel " + str(number))
    rows = await client.get_items("list")
    assert len(rows) == 151
    assert rows[0].name == name
    assert rows[0].description == quantity
    assert rows[0].metadata["version"] == 1
    assert [call[2].get("nextToken") for call in api.calls if call[1].path.endswith("/items/fetch")] == [None, "100"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_page_token", "duplicate_uid", "token_loop", "bad_shape"])
async def test_partial_or_ambiguous_reads_fail_instead_of_becoming_empty(failure):
    client, api, _ = connector()
    rows = [{"itemId": str(number), "itemName": "A", "itemStatus": "ACTIVE", "version": 1} for number in range(100)]
    if failure == "missing_page_token":
        api.pages = [{"itemInfoList": rows}]
    elif failure == "duplicate_uid":
        api.pages = [{"itemInfoList": rows, "nextToken": "x"}, {"itemInfoList": [rows[0]], "nextToken": None}]
    elif failure == "token_loop":
        api.pages = [{"itemInfoList": [], "nextToken": "x"}, {"itemInfoList": [], "nextToken": "x"}]
    else:
        api.pages = [{"unexpected": []}]
    with pytest.raises(ConnectorError):
        await client.get_items("list")


@pytest.mark.asyncio
async def test_create_matches_new_uid_and_preserves_existing_duplicates():
    client, api, _ = connector()
    api.add("already-there", "Milch")
    created = await client.add_item("list", "Milch")
    assert created.uid == "created-1"
    assert len(api.rows) == 2
    checked = await client.add_item("list", "USB-C", "2 STÜCK", completed=True)
    assert checked.fields() == {"name": "USB-C", "description": "2 STÜCK", "completed": True}
    assert len([call for call in api.calls if call[0] == "POST" and call[1].path.endswith("/items")]) == 2


@pytest.mark.asyncio
async def test_uncertain_create_recovers_uid_without_reposting():
    client, api, _ = connector()
    api.after_add_error = True
    with pytest.raises(ConnectorError) as caught:
        await client.add_item("list", "Milch", "1 L")
    assert "synthetic-secret" not in str(caught.value)
    recovered = await client.add_item("list", "Milch", "1 L")
    assert recovered.uid == "created-1"
    assert len(api.rows) == 1
    assert len([call for call in api.calls if call[0] == "POST" and call[1].path.endswith("/items")]) == 1


@pytest.mark.asyncio
async def test_engine_can_adopt_uncertain_create_on_read_then_add_another_item():
    client, api, _ = connector()
    api.after_add_error = True
    with pytest.raises(ConnectorError):
        await client.add_item("list", "Milch")
    assert len(await client.get_items("list")) == 1
    other = await client.add_item("list", "Eier")
    assert other.name == "Eier"
    assert len(api.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, 2])
async def test_unknown_create_missing_or_duplicate_candidate_never_blindly_retries(remaining):
    client, api, _ = connector()
    api.after_add_error = True
    with pytest.raises(ConnectorError):
        await client.add_item("list", "Milch")
    api.rows.clear()
    for index in range(remaining):
        api.add("candidate-" + str(index), "Milch")
    with pytest.raises(ConnectorError):
        await client.add_item("list", "Milch")
    assert len([call for call in api.calls if call[0] == "POST" and call[1].path.endswith("/items")]) == 1


@pytest.mark.asyncio
async def test_rename_checkbox_and_delete_use_fresh_versions_and_confirm():
    client, api, _ = connector()
    api.add("stable-uid", "Milch")
    current = (await client.get_items("list"))[0]
    updated = await client.update_item("list", current, name="HaferMILCH", description="½ L", completed=True)
    assert updated.uid == "stable-uid"
    assert updated.fields() == {"name": "HaferMILCH", "description": "½ L", "completed": True}
    versions = [call[1].query["version"] for call in api.calls if call[0] == "PUT"]
    assert versions == ["1", "2"]
    await client.delete_item("list", updated)
    assert not await client.get_items("list")


@pytest.mark.asyncio
async def test_concurrent_remote_edit_never_gets_overwritten():
    client, api, _ = connector()
    api.add("item", "Milch")
    current = (await client.get_items("list"))[0]
    api.rows["item"].update(itemName="Andere Änderung", version=2)
    with pytest.raises(ConnectorError):
        await client.update_item("list", current, name="Überschreiben")
    assert api.rows["item"]["itemName"] == "Andere Änderung"
    assert not any(call[0] == "PUT" for call in api.calls)


@pytest.mark.asyncio
async def test_auth_expiry_refreshes_once_and_saves_rotated_session_only():
    client, api, saved = connector()
    api.auth_errors = 1
    assert (await client.list_lists())[0].id == "list"
    assert api.login.cookie_calls == 1
    assert saved and set(saved[-1]) == {"email", "login_data"}
    assert saved[-1]["login_data"]["website_cookies"]["csrf"] == "synthetic-rotated-cookie"
    api.auth_errors = 2
    with pytest.raises(AuthenticationRequired) as caught:
        await client.list_lists()
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_login_returns_session_without_password_otp_or_debug_callback(monkeypatch):
    captured = {}
    def make_api(session, email, password, *, save_data, login_data):
        captured.update(password=password, callback=save_data.callback)
        return FakeAPI(session_data())
    monkeypatch.setattr(alexa, "AmazonEchoApi", make_api)
    result = await alexa.AlexaConnector.login(" user@example.invalid ", "synthetic-password", "123456")
    assert set(result) == {"email", "login_data"}
    assert "password" not in result["login_data"]
    assert "otp" not in result["login_data"]
    assert captured["callback"] is None
    assert result["email"] == "user@example.invalid"
    assert captured["password"] == "synthetic-password"


@pytest.mark.asyncio
async def test_stored_session_does_not_require_or_send_password_and_closes_owned_http(monkeypatch):
    captured = {}
    def make_api(session, email, password, *, save_data, login_data):
        captured.update(password=password, session=session, callback=save_data.callback)
        return FakeAPI(login_data)
    monkeypatch.setattr(alexa, "AmazonEchoApi", make_api)
    async def save(_):
        pass
    client = alexa.AlexaConnector({"email": "user@example.invalid", "login_data": session_data()}, save)
    try:
        await client.test_connection()
        assert captured["password"] == ""
        assert captured["callback"] is None
        assert not captured["session"].closed
    finally:
        await client.close()
    assert captured["session"].closed


@pytest.mark.asyncio
async def test_login_and_credential_storage_errors_never_expose_provider_secrets(monkeypatch):
    def fail_constructor(*args, **kwargs):
        raise RuntimeError("synthetic-token-must-not-leak")
    monkeypatch.setattr(alexa, "AmazonEchoApi", fail_constructor)
    with pytest.raises(ConnectorError) as caught:
        await alexa.AlexaConnector.login("user@example.invalid", "synthetic-password", "123456")
    assert "synthetic-" not in str(caught.value)
    client, api, _ = connector()
    async def fail_storage(_):
        raise RuntimeError("synthetic-password-must-not-leak")
    client._save_credentials = fail_storage
    api.login.data["access_token"] = "synthetic-rotated-token"
    with pytest.raises(ConnectorError) as caught:
        await client.list_lists()
    assert "synthetic-" not in str(caught.value)


def test_sdk_logging_never_reaches_application_handlers(caplog):
    connector()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("aioamazondevices").critical("synthetic-token-must-not-be-output")
        logging.getLogger("aioamazondevices.login").error("synthetic-cookie-must-not-be-output")
        logging.getLogger("listenbruecke.test").warning("Application diagnostics remain available")
    assert "synthetic-token" not in caplog.text
    assert "synthetic-cookie" not in caplog.text
    assert "Application diagnostics remain available" in caplog.text


@pytest.mark.asyncio
async def test_real_pinned_sdk_constructor_and_wrapper_use_no_network_or_debug_files(tmp_path):
    assert version("aioamazondevices") == "17.2.8"
    class NetworkStub:
        def __init__(self):
            self.cookie_jar = aiohttp.CookieJar()
            self.calls = []
        async def request(self, method, url, **kwargs):
            self.calls.append((method, url))
            return Response({}, status=503, url=url)
    network = NetworkStub()
    api = AmazonEchoApi(network, "user@example.invalid", "",
                        save_data=alexa.AmazonSaveDataConfig(path=tmp_path, callback=None),
                        login_data=session_data())
    parameters = inspect.signature(api._http_wrapper.session_request).parameters
    assert parameters["fail_fast"].kind is inspect.Parameter.KEYWORD_ONLY
    assert hasattr(api.login, "_refresh_auth_cookies")
    with pytest.raises(CannotRetrieveData):
        await api._http_wrapper.session_request("POST", URL("https://www.amazon.de/alexashoppinglists/api/v2/lists/fetch"),
                                                input_data={}, json_data=True, fail_fast=True)
    assert len(network.calls) == 1
    assert list(tmp_path.iterdir()) == []
