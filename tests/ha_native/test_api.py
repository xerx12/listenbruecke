"""Transport contract validation without manufacturer services or live sockets."""

from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.listenbruecke.api import ApiAuthError, ApiError, ListenbrueckeApi, normalize_url

from .conftest import BASE, TOKEN


@pytest.mark.parametrize("url", ["file:///private", "http://user:synthetic-password@server.test", "http://server.test:invalid", "http://server.test/../secret", "http://server.test/?token=secret"])
def test_url_validation_does_not_return_credentials(url):
    with pytest.raises(ApiError) as error:
        normalize_url(url)
    assert "synthetic-password" not in str(error.value)


async def test_invalid_and_duplicate_items_are_not_an_empty_list(hass, aioclient_mock):
    api = ListenbrueckeApi(async_get_clientsession(hass), BASE, TOKEN)
    aioclient_mock.get(BASE + "/api/ha/v1/items", json={"available": True})
    with pytest.raises(ApiError):
        await api.async_get_items()
    aioclient_mock.clear_requests()
    item = {"uid": "same-id", "summary": "Milch", "description": "1 l", "status": "needs_action"}
    aioclient_mock.get(BASE + "/api/ha/v1/items", json={"available": True, "items": [item, item]})
    with pytest.raises(ApiError):
        await api.async_get_items()


async def test_post_not_retried_and_remote_body_sanitized(hass, aioclient_mock):
    api = ListenbrueckeApi(async_get_clientsession(hass), BASE, TOKEN)
    aioclient_mock.post(BASE + "/api/ha/v1/items", status=503, json={"error": "synthetic-private-response"})
    with pytest.raises(ApiError) as error:
        await api.async_add_item("Milch", "1 l")
    assert "synthetic-private-response" not in str(error.value)
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][2]["request_id"]


async def test_bearer_token_is_not_followed_to_a_redirect(hass, aioclient_mock):
    api = ListenbrueckeApi(async_get_clientsession(hass), BASE, TOKEN)
    aioclient_mock.get(BASE + "/api/ha/v1/state", status=302, headers={"Location": "https://other.invalid/private"})
    with pytest.raises(ApiError):
        await api.async_get_state()
    assert aioclient_mock.call_count == 1


async def test_auth_failure_uses_a_dedicated_safe_error(hass, aioclient_mock):
    api = ListenbrueckeApi(async_get_clientsession(hass), BASE, TOKEN)
    aioclient_mock.get(BASE + "/api/ha/v1/state", status=401, json={"secret": TOKEN})
    with pytest.raises(ApiAuthError) as error:
        await api.async_get_state()
    assert TOKEN not in str(error.value)


async def test_provider_selection_is_validated_and_old_servers_remain_compatible(hass, aioclient_mock):
    from .conftest import FakeLocalApi
    api = ListenbrueckeApi(async_get_clientsession(hass), BASE, TOKEN)
    body = FakeLocalApi().state
    body["providers"][0]["selected"] = False
    aioclient_mock.get(BASE + "/api/ha/v1/state", json=body)
    parsed = await api.async_get_state()
    assert parsed["providers"][0]["selected"] is False
    assert parsed["providers"][1]["selected"] is True  # Older API: configured fallback.
    body["providers"][0]["selected"] = "false"
    aioclient_mock.clear_requests()
    aioclient_mock.get(BASE + "/api/ha/v1/state", json=body)
    with pytest.raises(ApiError):
        await api.async_get_state()
