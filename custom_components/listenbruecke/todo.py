"""Standard HA To-do entities, also usable through Assist and dashboards."""

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.exceptions import HomeAssistantError

from .entity import ListenbrueckeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ListenbrueckeTodo(entry.runtime_data)])


class ListenbrueckeTodo(ListenbrueckeEntity, TodoListEntity):
    _attr_translation_key = "shopping_list"
    _attr_icon = "mdi:cart"
    _attr_supported_features = (TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM
                                | TodoListEntityFeature.DELETE_TODO_ITEM | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)

    def __init__(self, coordinator):
        super().__init__(coordinator, "shopping_list")

    @property
    def available(self):
        return super().available and bool((self.coordinator.data or {}).get("items", {}).get("available"))

    @property
    def todo_items(self):
        if not self.available:
            return None
        return [TodoItem(uid=item["uid"], summary=item["summary"], description=item["description"],
                         status=TodoItemStatus(item["status"])) for item in self.coordinator.data["items"]["items"]]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        if not item.summary or not item.summary.strip():
            raise HomeAssistantError("Ein Einkaufsartikel benötigt einen Namen.")
        await self.async_call(self.coordinator.api.async_add_item(item.summary, item.description or ""))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if not item.uid or not item.summary or not item.summary.strip():
            raise HomeAssistantError("Der Einkaufsartikel konnte nicht eindeutig bestimmt werden.")
        await self.async_call(self.coordinator.api.async_update_item(
            item.uid, summary=item.summary, description=item.description or "",
            status=TodoItemStatus(item.status or TodoItemStatus.NEEDS_ACTION).value,
        ))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        async def remove():
            for uid in uids:
                await self.coordinator.api.async_delete_item(uid)
        await self.async_call(remove())
