"""Synchronization interval control for Listenbrücke."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the interval setting."""
    async_add_entities([IntervalNumber(entry.runtime_data)])


class IntervalNumber(ListenbrueckeEntity, NumberEntity):
    """Choose how often the app reconciles the selected shopping lists."""

    _attr_translation_key = "interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 30
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator):
        super().__init__(coordinator, "interval")

    @property
    def native_value(self):
        value = ((self.coordinator.data or {}).get("state", {}).get("sync") or {}).get("interval_seconds")
        return value if type(value) is int and 30 <= value <= 3600 else None

    async def async_set_native_value(self, value):
        await self.async_call(self.coordinator.api.async_update_settings(interval_seconds=int(value)))
