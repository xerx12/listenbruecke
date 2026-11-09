"""Shopping-list and synchronization sensors for Listenbrücke."""

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Add sensors for the configured Listenbrücke instance."""
    coordinator = entry.runtime_data
    async_add_entities([
        LastSuccessSensor(coordinator),
        OpenItemsSensor(coordinator),
        ItemsSyncedSensor(coordinator),
    ])


class LastSuccessSensor(ListenbrueckeEntity, SensorEntity):
    """Timestamp of the most recent successful reconciliation."""

    _attr_translation_key = "last_success"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_success")

    @property
    def native_value(self):
        value = ((self.coordinator.data or {}).get("state", {}).get("sync") or {}).get("last_success")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class OpenItemsSensor(ListenbrueckeEntity, SensorEntity):
    """Count incomplete entries in the shared shopping list."""

    _attr_translation_key = "open_items"
    _attr_icon = "mdi:cart-outline"

    def __init__(self, coordinator):
        super().__init__(coordinator, "open_items")

    @property
    def available(self):
        return super().available and bool((self.coordinator.data or {}).get("items", {}).get("available"))

    @property
    def native_value(self):
        data = (self.coordinator.data or {}).get("items", {})
        if not data.get("available"):
            return None
        return sum(item.get("status") == "needs_action" for item in data.get("items", []) if isinstance(item, dict))


class ItemsSyncedSensor(ListenbrueckeEntity, SensorEntity):
    """Cumulative confirmed additions and updates reported by the app."""

    _attr_translation_key = "items_synced"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator):
        super().__init__(coordinator, "items_synced")

    @property
    def native_value(self):
        value = ((self.coordinator.data or {}).get("state", {}).get("sync") or {}).get("items_synced")
        return value if type(value) is int and value >= 0 else None
