"""Stable entities on one Listenbrücke device, with safe service errors."""

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ApiAuthError, ApiError
from .const import DOMAIN
from .coordinator import ListenbrueckeCoordinator


class ListenbrueckeEntity(CoordinatorEntity[ListenbrueckeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ListenbrueckeCoordinator, key: str) -> None:
        super().__init__(coordinator, context=key)
        self._attr_unique_id = f"{coordinator.instance_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        state = (self.coordinator.data or {}).get("state", {})
        return DeviceInfo(identifiers={(DOMAIN, self.coordinator.instance_id)}, name="Listenbrücke",
                          manufacturer="Listenbrücke", model="Einkaufslisten-Brücke",
                          sw_version=state.get("version"))

    async def async_call(self, operation):
        try:
            return await operation
        except ApiAuthError:
            raise HomeAssistantError("Das Listenbrücke-App-Token ist ungültig. Bitte die Integration erneut verbinden.") from None
        except ApiError:
            raise HomeAssistantError("Die Listenbrücke-App konnte die Änderung nicht bestätigen. Bitte die Einkaufsliste prüfen.") from None
        finally:
            # A later write in a compound operation may fail after an earlier
            # one succeeded. Await an actual read, including during HA's refresh
            # cooldown, so dashboard/Assist services immediately see the write.
            # This refresh never retries the preceding mutation.
            await self.coordinator.async_refresh()
