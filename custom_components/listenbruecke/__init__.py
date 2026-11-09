"""Native local integration for the independently hosted Listenbrücke app."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ListenbrueckeApi
from .const import CONF_TOKEN, CONF_URL, PLATFORMS
from .coordinator import ListenbrueckeCoordinator

type ListenbrueckeConfigEntry = ConfigEntry[ListenbrueckeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ListenbrueckeConfigEntry) -> bool:
    api = ListenbrueckeApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    coordinator = ListenbrueckeCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ListenbrueckeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
