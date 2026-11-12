"""Provider connections and problem reporting for Listenbrücke."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Add connection diagnostics and the shared problem indicator."""
    coordinator = entry.runtime_data
    async_add_entities([ProblemSensor(coordinator)])
    known = set()

    @callback
    def add_new_providers():
        entities = []
        for provider in (coordinator.data or {}).get("state", {}).get("providers", []):
            if provider["id"] not in known:
                known.add(provider["id"])
                entities.append(ProviderConnectionSensor(coordinator, provider["id"], provider["name"]))
        if entities:
            async_add_entities(entities)

    add_new_providers()
    entry.async_on_unload(coordinator.async_add_listener(add_new_providers))


class ProblemSensor(ListenbrueckeEntity, BinarySensorEntity):
    """Report synchronization failures or an unavailable configured provider."""

    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "problem")

    @property
    def is_on(self):
        state = (self.coordinator.data or {}).get("state", {})
        if (state.get("sync") or {}).get("last_error"):
            return True
        return any(
            provider.get("selected", provider.get("configured")) and (provider.get("error") or not provider.get("connected"))
            for provider in state.get("providers", []) if isinstance(provider, dict)
        )


class ProviderConnectionSensor(ListenbrueckeEntity, BinarySensorEntity):
    """Expose one provider's current connection status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, provider, name):
        super().__init__(coordinator, "provider_" + provider)
        self._provider = provider
        self._attr_translation_key = "provider_connection"
        self._attr_translation_placeholders = {"provider": name}

    @property
    def available(self):
        return super().available and any(
            provider["id"] == self._provider
            for provider in (self.coordinator.data or {}).get("state", {}).get("providers", [])
        )

    @property
    def is_on(self):
        providers = (self.coordinator.data or {}).get("state", {}).get("providers", [])
        return any(
            provider.get("id") == self._provider and bool(provider.get("connected"))
            for provider in providers if isinstance(provider, dict)
        )
