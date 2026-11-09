"""On-demand shopping-list reconciliation for Listenbrücke."""

from homeassistant.components.button import ButtonEntity

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the manual synchronization action."""
    async_add_entities([SyncNowButton(entry.runtime_data)])


class SyncNowButton(ListenbrueckeEntity, ButtonEntity):
    """Start a background synchronization in the app."""

    _attr_translation_key = "sync_now"
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator):
        super().__init__(coordinator, "sync_now")

    async def async_press(self):
        await self.async_call(self.coordinator.api.async_sync())
