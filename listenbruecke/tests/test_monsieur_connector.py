"""Synthetic provider responses; no account or live HTTP access."""

from copy import deepcopy

import pytest

from listenbruecke.connectors.monsieur import MonsieurConnector
from listenbruecke.models import ConnectorError, Item, UnsupportedOperation


def shopping():
    return {"shoppingListRecipes": [{"id": 10, "recipe": {"name": "Testsuppe", "ingredients": [
        {"id": 101, "name": "Karotte", "amount": "½", "unit": "kg", "isTicked": False},
    ]}}], "shoppingListCustomItems": [
        {"id": 201, "name": "Milch", "amount": "1 l", "isTicked": False},
        {"id": 202, "name": "Milch", "amount": "2 l", "isTicked": True},
    ]}


class FakeAuth:
    def __init__(self, data=None):
        self.data = shopping() if data is None else data
        self.calls = []
        self.confirm_writes = True

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if method == "GET":
            return {"code": 0, "data": deepcopy(self.data)}
        if not self.confirm_writes:
            return {"code": 0, "data": {}}
        payload = kwargs.get("json", {})
        if method == "POST" and path.endswith("customItems"):
            item = {"id": 203, **payload, "isTicked": False}
            self.data["shoppingListCustomItems"].append(item)
            return {"code": 0, "data": {"shoppingListCustomItem": deepcopy(item)}}
        if path.endswith("recipes/ingredients/bulk"):
            for row in self.data["shoppingListRecipes"]:
                for item in row["recipe"]["ingredients"]:
                    for patch in payload:
                        if item["id"] == patch["id"]:
                            item.update(patch)
        elif method == "PATCH":
            item = next(i for i in self.data["shoppingListCustomItems"] if str(i["id"]) == path.rsplit("/", 1)[1])
            item.update(payload)
            return {"code": 0, "data": {"shoppingListCustomItem": deepcopy(item)}}
        elif method == "DELETE":
            self.data["shoppingListCustomItems"] = [i for i in self.data["shoppingListCustomItems"] if str(i["id"]) != path.rsplit("/", 1)[1]]
        return {"code": 0, "data": {}}


@pytest.mark.asyncio
async def test_maps_actual_shopping_list_and_keeps_duplicates_fractions():
    connector = MonsieurConnector(FakeAuth())
    items = await connector.get_items("shopping")
    assert [i.uid for i in items] == ["r:10:101", "c:201", "c:202"]
    assert items[0].description == "½ kg"
    assert not items[0].editable and not items[0].deletable
    assert items[1].name == items[2].name == "Milch"
    assert items[2].completed


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"shoppingListRecipes": [], "shoppingListCustomItems": None},
                                  {"shoppingListRecipes": None, "shoppingListCustomItems": []}])
async def test_invalid_response_never_becomes_empty_list(data):
    with pytest.raises(ConnectorError):
        await MonsieurConnector(FakeAuth(data)).get_items("shopping")


@pytest.mark.asyncio
async def test_custom_crud_targets_exact_id_and_confirms_state():
    auth = FakeAuth()
    connector = MonsieurConnector(auth)
    items = await connector.get_items("shopping")
    changed = await connector.update_item("shopping", items[1], description="3 l", completed=True)
    assert changed.description == "3 l" and changed.completed
    assert auth.data["shoppingListCustomItems"][1]["amount"] == "2 l"
    patch = next(c for c in auth.calls if c[0] == "PATCH")
    assert patch[1].endswith("/201")
    assert patch[2]["json"] == {"amount": "3 l", "isTicked": True}
    added = await connector.add_item("shopping", "Brot", "1 Stück", completed=True)
    assert added.uid == "c:203" and added.completed
    await connector.delete_item("shopping", added)
    assert added.uid not in [i.uid for i in await connector.get_items("shopping")]


@pytest.mark.asyncio
async def test_recipe_checkbox_is_allowed_but_no_guessing_edit_or_delete():
    auth = FakeAuth()
    connector = MonsieurConnector(auth)
    recipe_item = (await connector.get_items("shopping"))[0]
    updated = await connector.update_item("shopping", recipe_item, completed=True)
    assert updated.completed
    bulk = next(c for c in auth.calls if c[1].endswith("/bulk"))
    assert bulk[2]["json"][0]["id"] == 101
    assert set(bulk[2]["json"][0]) == {"id", "isTicked", "lastUpdateTime"}
    with pytest.raises(UnsupportedOperation):
        await connector.update_item("shopping", updated, description="1 kg")
    with pytest.raises(UnsupportedOperation):
        await connector.delete_item("shopping", updated)
    assert not any(c[0] == "DELETE" for c in auth.calls)


@pytest.mark.asyncio
async def test_wrong_list_stale_item_and_unconfirmed_write_fail_closed():
    auth = FakeAuth()
    connector = MonsieurConnector(auth)
    with pytest.raises(ConnectorError):
        await connector.get_items("some_other_list")
    assert not auth.calls
    with pytest.raises(ConnectorError):
        await connector.update_item("shopping", Item("c:999", "Weg"), completed=True)
    auth.confirm_writes = False
    item = (await connector.get_items("shopping"))[1]
    with pytest.raises(ConnectorError, match="bestätigt"):
        await connector.update_item("shopping", item, completed=True)


@pytest.mark.asyncio
async def test_repeated_recipe_ingredient_api_id_is_not_edited_ambiguously():
    auth = FakeAuth()
    other = deepcopy(auth.data["shoppingListRecipes"][0])
    other["id"] = 11
    auth.data["shoppingListRecipes"].append(other)
    connector = MonsieurConnector(auth)
    rows = await connector.get_items("shopping")
    assert rows[0].uid != rows[1].uid
    with pytest.raises(UnsupportedOperation, match="eindeutige"):
        await connector.update_item("shopping", rows[0], completed=True)
    assert not any(c[0] == "POST" for c in auth.calls)
