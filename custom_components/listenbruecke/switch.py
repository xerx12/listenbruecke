"""Automatic reconciliation control for Listenbrücke."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the automatic synchronization switch."""
    async_add_entities([AutoSyncSwitch(entry.runtime_data)])


class AutoSyncSwitch(ListenbrueckeEntity, SwitchEntity):
    """Enable or pause automatic synchronization."""

    _attr_translation_key = "auto_sync"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator):
        super().__init__(coordinator, "auto_sync")

    @property
    def is_on(self):
        return bool(((self.coordinator.data or {}).get("state", {}).get("sync") or {}).get("enabled"))

    async def async_turn_on(self, **kwargs):
        await self.async_call(self.coordinator.api.async_update_settings(enabled=True))

    async def async_turn_off(self, **kwargs):
        await self.async_call(self.coordinator.api.async_update_settings(enabled=False))
