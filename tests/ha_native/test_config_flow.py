"""Pairing, duplicate instance prevention, and safe reauthentication."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.listenbruecke.const import CONF_TOKEN, CONF_URL, DOMAIN

from .conftest import BASE, INSTANCE, TOKEN


async def test_pairing_creates_unique_instance_entry(hass, local_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.listenbruecke.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE + "/", CONF_TOKEN: TOKEN})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: BASE, CONF_TOKEN: TOKEN}
    assert result["result"].unique_id == INSTANCE
    assert not any(name in result["data"] for name in ("password", "email", "otp", "code"))


async def test_duplicate_app_instance_is_not_added(hass, local_api, config_entry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                    data={CONF_URL: BASE, CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_token_and_connection_errors_are_safe(hass, local_api):
    local_api.status = 401
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                    data={CONF_URL: BASE, CONF_TOKEN: TOKEN})
    assert result["errors"] == {"base": "invalid_auth"}
    local_api.status = 503
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE, CONF_TOKEN: TOKEN})
    assert result["errors"] == {"base": "cannot_connect"}
    assert TOKEN not in str(result["errors"])


async def test_reauth_checks_instance_identity_and_updates_token(hass, local_api, config_entry):
    result = await hass.config_entries.flow.async_init(DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id}, data=config_entry.data)
    assert result["step_id"] == "reauth_confirm"
    local_api.state["instance_id"] = "a-different-app"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE, CONF_TOKEN: TOKEN})
    assert result["errors"] == {"base": "wrong_instance"}
    local_api.state["instance_id"] = INSTANCE
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", new=AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: BASE, CONF_TOKEN: TOKEN})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
