"""Pair Home Assistant with a local app using a dedicated app token."""

import probatio

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import ApiAuthError, ApiError, ListenbrueckeApi
from .const import CONF_TOKEN, CONF_URL, DOMAIN

SCHEMA = probatio.Schema({
    probatio.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
    probatio.Required(probatio.Secret(CONF_TOKEN)): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")),
})


class ListenbrueckeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user_input):
        try:
            api = ListenbrueckeApi(async_get_clientsession(self.hass), user_input[CONF_URL], user_input[CONF_TOKEN])
            state = await api.async_get_state()
        except ApiAuthError:
            return {"base": "invalid_auth"}
        except ApiError:
            return {"base": "cannot_connect"}
        self._validated = {CONF_URL: api.base_url, CONF_TOKEN: user_input[CONF_TOKEN]}
        await self.async_set_unique_id(state["instance_id"])
        return {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None and not (errors := await self._validate(user_input)):
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Listenbrücke", data=self._validated)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None and not (errors := await self._validate(user_input)):
            if self.unique_id != entry.unique_id:
                errors = {"base": "wrong_instance"}
            else:
                return self.async_update_reload_and_abort(entry, data_updates=self._validated)
        return self.async_show_form(
            step_id="reauth_confirm", errors=errors,
            data_schema=self.add_suggested_values_to_schema(SCHEMA, {CONF_URL: entry.data[CONF_URL]}),
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None and not (errors := await self._validate(user_input)):
            if self.unique_id != entry.unique_id:
                errors = {"base": "wrong_instance"}
            else:
                return self.async_update_reload_and_abort(entry, data_updates=self._validated)
        return self.async_show_form(
            step_id="reconfigure", errors=errors,
            data_schema=self.add_suggested_values_to_schema(SCHEMA, {CONF_URL: entry.data[CONF_URL]}),
        )
