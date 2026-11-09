"""One local polling coordinator per paired app instance."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApiAuthError, ApiError, ListenbrueckeApi
from .const import DOMAIN, UPDATE_INTERVAL

LOGGER = logging.getLogger(__name__)


class ListenbrueckeCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry: ConfigEntry, api: ListenbrueckeApi) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=UPDATE_INTERVAL, always_update=False)
        self.api = api
        self.instance_id = entry.unique_id

    async def _async_update_data(self) -> dict:
        try:
            state = await self.api.async_get_state()
            if state["instance_id"] != self.instance_id:
                raise ApiAuthError("Die Adresse gehört zu einer anderen Listenbrücke-Instanz.")
            items = await self.api.async_get_items()
            return {"state": state, "items": items}
        except ApiAuthError:
            raise ConfigEntryAuthFailed("Bitte die Listenbrücke-App erneut mit ihrem Home-Assistant-Token verbinden.") from None
        except ApiError:
            raise UpdateFailed("Die Listenbrücke-App konnte nicht aktualisiert werden.") from None
