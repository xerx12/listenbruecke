"""Exercise actual HA entity platforms and standard dashboard/Assist services."""

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.listenbruecke.const import DOMAIN

from .conftest import INSTANCE


def entity_id(hass, domain, key):
    result = er.async_get(hass).async_get_entity_id(domain, DOMAIN, INSTANCE + "_" + key)
    assert result
    return result


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_one_device_native_entities_and_clean_unload(hass, local_api, config_entry):
    await setup(hass, config_entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, INSTANCE), config_entry.entry_id)
    assert device and device.name == "Listenbrücke"
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert len(entries) == 12
    assert {entry.device_id for entry in entries} == {device.id}
    assert hass.states.get(entity_id(hass, "sensor", "open_items")).state == "1"
    assert hass.states.get(entity_id(hass, "sensor", "items_synced")).state == "42"
    assert hass.states.get(entity_id(hass, "sensor", "last_success")).state == "2026-11-09T18:30:00+00:00"
    assert hass.states.get(entity_id(hass, "binary_sensor", "problem")).state == "off"
    assert config_entry.runtime_data.update_interval.total_seconds() == 30
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "todo", "shopping_list")).state == STATE_UNAVAILABLE
    original_ids = {entry.entity_id for entry in entries}
    await setup(hass, config_entry)
    reloaded = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert {entry.entity_id for entry in reloaded} == original_ids
    assert {entry.device_id for entry in reloaded} == {device.id}


async def test_standard_todo_add_update_description_status_and_delete_exact_uid(hass, local_api, config_entry):
    await setup(hass, config_entry)
    todo = entity_id(hass, "todo", "shopping_list")
    await hass.services.async_call("todo", "add_item", {"entity_id": todo, "item": "Brot", "description": "1 Stück"}, blocking=True)
    assert local_api.items[-1] == {"uid": "item-c", "summary": "Brot", "description": "1 Stück", "status": "needs_action"}
    await hass.services.async_call("todo", "update_item", {"entity_id": todo, "item": "item-b", "rename": "Haferdrink", "description": "3 l", "status": "needs_action"}, blocking=True)
    assert local_api.items[0]["summary"] == "Milch"
    assert local_api.items[1] == {"uid": "item-b", "summary": "Haferdrink", "description": "3 l", "status": "needs_action"}
    await hass.services.async_call("todo", "remove_item", {"entity_id": todo, "item": "item-a"}, blocking=True)
    assert [item["uid"] for item in local_api.items] == ["item-b", "item-c"]
    mutations = [(method, path) for method, path, _ in local_api.calls if method != "GET"]
    assert mutations == [("POST", "items"), ("PATCH", "items/item-b"), ("DELETE", "items/item-a")]
    assert hass.states.get(todo).state == "2"


async def test_switch_number_and_button_use_partial_settings_and_single_posts(hass, local_api, config_entry):
    await setup(hass, config_entry)
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id(hass, "switch", "auto_sync")}, blocking=True)
    assert local_api.state["sync"]["enabled"] is False
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id(hass, "number", "interval"), "value": 120}, blocking=True)
    assert local_api.state["sync"]["interval_seconds"] == 120
    await hass.services.async_call("button", "press", {"entity_id": entity_id(hass, "button", "sync_now")}, blocking=True)
    assert [(method, path, body) for method, path, body in local_api.calls if method != "GET"] == [
        ("PUT", "settings", {"enabled": False}), ("PUT", "settings", {"interval_seconds": 120}), ("POST", "sync", {}),
    ]


async def test_provider_problem_and_unavailable_items_preserve_state(hass, local_api, config_entry):
    await setup(hass, config_entry)
    local_api.state["providers"][2]["connected"] = False
    local_api.state["providers"][2]["error"] = "Synthetic provider unavailable"
    local_api.available = False
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "binary_sensor", "problem")).state == "on"
    assert hass.states.get(entity_id(hass, "binary_sensor", "provider_alexa")).state == "off"
    assert hass.states.get(entity_id(hass, "todo", "shopping_list")).state == STATE_UNAVAILABLE
    assert hass.states.get(entity_id(hass, "sensor", "open_items")).state == STATE_UNAVAILABLE
    assert len(config_entry.runtime_data.data["items"]["items"]) == 2


async def test_api_outage_marks_entities_unavailable_and_auth_failure_starts_reauth(hass, local_api, config_entry):
    await setup(hass, config_entry)
    local_api.status = 503
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert not config_entry.runtime_data.last_update_success
    assert hass.states.get(entity_id(hass, "todo", "shopping_list")).state == STATE_UNAVAILABLE
    assert hass.states.get(entity_id(hass, "switch", "auto_sync")).state == STATE_UNAVAILABLE
    local_api.status = 401
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"


async def test_service_failure_is_sanitized_without_automatic_write_retry(hass, local_api, config_entry):
    await setup(hass, config_entry)
    local_api.write_status = 503
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call("todo", "add_item", {"entity_id": entity_id(hass, "todo", "shopping_list"), "item": "Brot"}, blocking=True)
    assert "synthetic-private-response" not in str(error.value)
    assert len([call for call in local_api.calls if call[:2] == ("POST", "items")]) == 1


async def test_compound_delete_refreshes_after_partial_failure(hass, local_api, config_entry):
    await setup(hass, config_entry)
    local_api.fail_paths = {"items/item-b"}
    todo = entity_id(hass, "todo", "shopping_list")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("todo", "remove_item", {"entity_id": todo, "item": ["item-a", "item-b"]}, blocking=True)
    assert [item["uid"] for item in local_api.items] == ["item-b"]
    assert [item["uid"] for item in config_entry.runtime_data.data["items"]["items"]] == ["item-b"]


async def test_fractional_interval_is_rejected_without_silent_truncation(hass, local_api, config_entry):
    await setup(hass, config_entry)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("number", "set_value", {"entity_id": entity_id(hass, "number", "interval"), "value": 30.5}, blocking=True)
    assert not any(method == "PUT" for method, _, _ in local_api.calls)


async def test_initial_api_failure_retries_setup_without_fabricating_empty_list(hass, local_api, config_entry):
    local_api.status = 503
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.states.async_all("todo")
