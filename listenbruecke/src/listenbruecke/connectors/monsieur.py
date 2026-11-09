"""Shopping-list adapter for the user's Monsieur Cuisine cloud account.

Paths and payloads mirror the public Monsieur Cuisine web frontend. Recipe
ingredients deliberately expose checkbox changes only: the frontend provides
no individual ingredient rename, quantity-edit, or deletion operation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from listenbruecke.models import ConnectorError, Item, ShoppingList, UnsupportedOperation

if TYPE_CHECKING:
    from .mc_auth import MCAuth

_PATH = "api/v1/users/shoppingList"
_FORMAT_ERROR = "Die Monsieur-Cuisine-Einkaufsliste hat ein unbekanntes Datenformat."
_CONFIRM_ERROR = "Monsieur Cuisine hat die Änderung noch nicht bestätigt. Bitte die Liste neu laden."


def _data(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        raise ConnectorError(_FORMAT_ERROR)
    data = body.get("data")
    if not isinstance(data, dict):
        raise ConnectorError(_FORMAT_ERROR)
    return data


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
        raise ConnectorError(_FORMAT_ERROR)
    return str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConnectorError(_FORMAT_ERROR)
    return str(value)


def _completed(value: Any) -> bool:
    if value not in (False, True, 0, 1):
        raise ConnectorError(_FORMAT_ERROR)
    return bool(value)


class MonsieurConnector:
    def __init__(self, auth: MCAuth) -> None:
        self.auth = auth
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _list(list_id: str) -> None:
        if list_id != "shopping":
            raise ConnectorError("Die ausgewählte Monsieur-Cuisine-Liste existiert nicht.")

    async def test_connection(self) -> list[ShoppingList]:
        await self.get_items("shopping")
        return await self.list_lists()

    async def list_lists(self) -> list[ShoppingList]:
        return [ShoppingList("shopping", "Monsieur Cuisine")]

    async def get_items(self, list_id: str) -> list[Item]:
        self._list(list_id)
        data = _data(await self.auth.request("GET", _PATH))
        recipes, customs = data.get("shoppingListRecipes"), data.get("shoppingListCustomItems")
        if not isinstance(recipes, list) or not isinstance(customs, list):
            raise ConnectorError(_FORMAT_ERROR)
        items: list[Item] = []
        for row in recipes:
            if not isinstance(row, dict) or not isinstance(row.get("recipe"), dict):
                raise ConnectorError(_FORMAT_ERROR)
            row_id = _identifier(row.get("id"))
            recipe = row["recipe"]
            ingredients = recipe.get("ingredients")
            if not isinstance(ingredients, list):
                raise ConnectorError(_FORMAT_ERROR)
            for ingredient in ingredients:
                if not isinstance(ingredient, dict):
                    raise ConnectorError(_FORMAT_ERROR)
                ingredient_id = _identifier(ingredient.get("id"))
                name = ingredient.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ConnectorError(_FORMAT_ERROR)
                amount, unit = _text(ingredient.get("amount")), _text(ingredient.get("unit"))
                items.append(Item(
                    uid=f"r:{row_id}:{ingredient_id}", name=name,
                    description=" ".join(p for p in (amount.strip(), unit.strip()) if p),
                    completed=_completed(ingredient.get("isTicked")), editable=False, deletable=False,
                    metadata={"kind": "recipe", "raw_id": ingredient["id"],
                              "recipe_row_id": row["id"], "recipe_name": _text(recipe.get("name")),
                              "amount": amount, "unit": unit},
                ))
        for custom in customs:
            if not isinstance(custom, dict):
                raise ConnectorError(_FORMAT_ERROR)
            custom_id = _identifier(custom.get("id"))
            name = custom.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ConnectorError(_FORMAT_ERROR)
            items.append(Item(
                uid=f"c:{custom_id}", name=name, description=_text(custom.get("amount")),
                completed=_completed(custom.get("isTicked")),
                metadata={"kind": "custom", "raw_id": custom["id"]},
            ))
        if len({i.uid for i in items}) != len(items):
            raise ConnectorError(_FORMAT_ERROR)
        ingredient_ids = [str(i.metadata["raw_id"]) for i in items if i.metadata["kind"] == "recipe"]
        ambiguous_ids = {uid for uid in ingredient_ids if ingredient_ids.count(uid) > 1}
        for item in items:
            if item.metadata["kind"] == "recipe" and str(item.metadata["raw_id"]) in ambiguous_ids:
                item.metadata["ambiguous_api_id"] = True
        return items

    async def _current(self, list_id: str, item: Item) -> Item:
        current = next((i for i in await self.get_items(list_id) if i.uid == item.uid), None)
        if current is None:
            raise ConnectorError("Der Monsieur-Cuisine-Eintrag wurde inzwischen entfernt.")
        return current

    async def _confirm(self, list_id: str, uid: str, name: str, description: str, completed: bool) -> Item:
        current = next((i for i in await self.get_items(list_id) if i.uid == uid), None)
        if current is None or current.fields() != {
            "name": name, "description": description, "completed": completed,
        }:
            raise ConnectorError(_CONFIRM_ERROR)
        return current

    async def add_item(self, list_id: str, name: str, description: str = "", completed: bool = False) -> Item:
        self._list(list_id)
        if not name.strip():
            raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
        async with self._write_lock:
            body = _data(await self.auth.request("POST", f"{_PATH}/customItems", json={"name": name, "amount": description}))
            custom = body.get("shoppingListCustomItem")
            if not isinstance(custom, dict):
                raise ConnectorError(_CONFIRM_ERROR)
            raw_id = _identifier(custom.get("id"))
            if completed:
                await self.auth.request("PATCH", f"{_PATH}/customItems/{raw_id}", json={"isTicked": True})
            return await self._confirm(list_id, f"c:{raw_id}", name, description, completed)

    async def update_item(self, list_id: str, item: Item, *, name: str | None = None,
                          description: str | None = None, completed: bool | None = None) -> Item:
        self._list(list_id)
        async with self._write_lock:
            current = await self._current(list_id, item)
            new_name = current.name if name is None else name
            new_description = current.description if description is None else description
            new_completed = current.completed if completed is None else completed
            if not new_name.strip():
                raise ConnectorError("Ein Einkaufsartikel benötigt einen Namen.")
            if current.metadata["kind"] == "recipe":
                if new_name != current.name or new_description != current.description:
                    raise UnsupportedOperation("Bei Monsieur-Cuisine-Rezeptzutaten kann nur der Haken geändert werden.")
                if new_completed != current.completed:
                    if current.metadata.get("ambiguous_api_id"):
                        raise UnsupportedOperation("Diese Monsieur-Cuisine-Zutat besitzt keine eindeutige Kennung und kann nicht sicher geändert werden.")
                    await self.auth.request("POST", f"{_PATH}/recipes/ingredients/bulk", json=[{
                        "id": current.metadata["raw_id"], "isTicked": new_completed,
                        "lastUpdateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }])
            else:
                patch: dict[str, Any] = {}
                if new_name != current.name:
                    patch["name"] = new_name
                if new_description != current.description:
                    patch["amount"] = new_description
                if new_completed != current.completed:
                    patch["isTicked"] = new_completed
                if patch:
                    await self.auth.request("PATCH", f"{_PATH}/customItems/{_identifier(current.metadata['raw_id'])}", json=patch)
            return await self._confirm(list_id, current.uid, new_name, new_description, new_completed)

    async def delete_item(self, list_id: str, item: Item) -> None:
        self._list(list_id)
        async with self._write_lock:
            current = await self._current(list_id, item)
            if current.metadata["kind"] != "custom":
                raise UnsupportedOperation("Einzelne Rezeptzutaten können bei Monsieur Cuisine nicht gelöscht werden.")
            await self.auth.request("DELETE", f"{_PATH}/customItems/{_identifier(current.metadata['raw_id'])}")
            if any(i.uid == current.uid for i in await self.get_items(list_id)):
                raise ConnectorError(_CONFIRM_ERROR)

    async def close(self) -> None:
        # The shared HTTP client and authentication lifecycle belong to the app.
        return None
