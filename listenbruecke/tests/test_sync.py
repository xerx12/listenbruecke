"""Durable reconciliation tests using independent in-memory provider lists."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from listenbruecke.models import ConnectorError, Item, UnsupportedOperation
from listenbruecke.store import Store
from listenbruecke.sync import SyncEngine


class FakeConnector:
    def __init__(self, *items):
        self.items = {item.uid: item for item in items}
        self.calls = []
        self.counter = 0
        self.unavailable = False
        self.fail_add_after_write = False
        self.fail_update_before_write = False
        self.fail_update_after_write = False
        self.rename_replaces_uid = False

    async def get_items(self, list_id):
        if self.unavailable:
            raise ConnectorError("Quelle nicht erreichbar.")
        return list(self.items.values())

    async def add_item(self, list_id, name, description="", completed=False):
        self.calls.append(("add", name, description, completed))
        self.counter += 1
        item = Item(f"new-{id(self)}-{self.counter}", name, description, completed)
        self.items[item.uid] = item
        if self.fail_add_after_write:
            self.fail_add_after_write = False
            raise ConnectorError("Antwort nach dem Schreiben unterbrochen.")
        return item

    async def update_item(self, list_id, item, **values):
        self.calls.append(("update", item.uid, values))
        if self.fail_update_before_write:
            self.fail_update_before_write = False
            raise ConnectorError("Schreiben unterbrochen.")
        if not item.editable and any(key in values for key in ("name", "description")):
            raise UnsupportedOperation("Geschützter Rezeptbestandteil.")
        new = replace(item, **values)
        if self.rename_replaces_uid and new.name != item.name:
            new = replace(new, uid=item.uid + "-renamed")
            del self.items[item.uid]
        self.items[new.uid] = new
        if self.fail_update_after_write:
            self.fail_update_after_write = False
            raise ConnectorError("Antwort nach Änderung unterbrochen.")
        return new

    async def delete_item(self, list_id, item):
        self.calls.append(("delete", item.uid))
        if not item.deletable:
            raise UnsupportedOperation("Geschützter Rezeptbestandteil.")
        self.items.pop(item.uid, None)

    def change(self, uid, **values):
        self.items[uid] = replace(self.items[uid], **values)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.engine = SyncEngine(self.store)
        self.targets = [{"provider": name, "list_id": "list"} for name in ("a", "b")]

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def run_pair(self, a, b, **kwargs):
        return await self.engine.run(self.targets, {"a": a, "b": b}, **kwargs)

    def restart(self):
        self.store.close()
        self.store = Store(Path(self.directory.name))
        self.engine = SyncEngine(self.store)

    async def test_union_normalizes_exact_labels_and_preserves_occurrences_and_quantity(self):
        a = FakeConnector(Item("a1", "  MILCH  ", "½ l"), Item("a2", "Milch", "½ l"), Item("a3", "Brot"))
        b = FakeConnector(Item("b1", "milch", "½ l"), Item("b2", "Eier", "6 Stück"))
        result = await self.run_pair(a, b)
        self.assertEqual(result["added"], 3)
        self.assertEqual(len(a.items), 4)
        self.assertEqual(len(b.items), 4)
        self.assertEqual(sum(item.description == "½ l" for item in b.items.values()), 2)
        self.assertTrue(any(item.description == "6 Stück" for item in a.items.values()))
        again = await self.run_pair(a, b)
        self.assertEqual((again["added"], again["updated"]), (0, 0))

    async def test_historical_completed_ignored_until_explicitly_reopened(self):
        a, b = FakeConnector(Item("old", "Milch", completed=True)), FakeConnector()
        self.assertEqual((await self.run_pair(a, b))["added"], 0)
        self.assertFalse(b.items)
        self.restart()
        a.change("old", completed=False)
        self.assertEqual((await self.run_pair(a, b))["added"], 1)
        self.assertFalse(next(iter(b.items.values())).completed)

    async def test_completion_and_reopen_propagate_both_ways_across_restart(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        a.change("a", completed=True)
        await self.run_pair(a, b)
        self.assertTrue(b.items["b"].completed)
        self.restart()
        self.assertEqual((await self.run_pair(a, b))["updated"], 0)
        self.assertTrue(a.items["a"].completed)
        b.change("b", completed=False)
        await self.run_pair(a, b)
        self.assertFalse(a.items["a"].completed)

    async def test_same_bring_list_exposed_through_ha_is_blocked_before_writes(self):
        targets = [{"provider": "bring", "list_id": "native"}, {"provider": "homeassistant", "list_id": "todo.bring"}]
        a = FakeConnector(Item("same-uuid", "Milch"), Item("extra", "Eier"))
        b = FakeConnector(Item("same-uuid", "Milch"))
        with self.assertRaises(ConnectorError):
            await self.engine.run(targets, {"bring": a, "homeassistant": b})
        self.assertEqual(a.calls + b.calls, [])

    async def test_concurrent_conflicting_edits_never_overwrite_either_side(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        a.change("a", name="Hafermilch")
        b.change("b", name="Sojamilch")
        result = await self.run_pair(a, b)
        self.assertFalse(result["ok"])
        self.assertEqual((a.items["a"].name, b.items["b"].name), ("Hafermilch", "Sojamilch"))
        self.assertEqual(a.calls + b.calls, [])

    async def test_manually_aligned_pending_conflict_can_be_resolved(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        a.change("a", name="Hafermilch")
        b.fail_update_before_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        b.change("b", name="Sojamilch")
        self.assertFalse((await self.run_pair(a, b))["ok"])
        a.change("a", name="Sojamilch")
        self.assertTrue((await self.run_pair(a, b))["ok"])
        self.assertEqual((a.items["a"].name, b.items["b"].name), ("Sojamilch", "Sojamilch"))

    async def test_failed_source_read_never_becomes_empty_list_or_deletion(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        a.unavailable = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b, propagate_deletions=True)
        self.assertEqual(a.calls + b.calls, [])
        self.assertIn("b", b.items)

    async def test_deletions_off_suppress_removed_copy_without_resurrection(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        del a.items["a"]
        await self.run_pair(a, b)
        self.restart()
        b.change("b", completed=True)
        await self.run_pair(a, b)
        self.assertFalse(a.items)
        self.assertIn("b", b.items)
        self.assertEqual(a.calls + b.calls, [])

    async def test_deletions_on_remove_other_copies_and_remain_retired(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        del a.items["a"]
        self.assertEqual((await self.run_pair(a, b, propagate_deletions=True))["removed"], 1)
        self.restart()
        self.assertEqual((await self.run_pair(a, b, propagate_deletions=True))["added"], 0)
        self.assertFalse(a.items or b.items)

    async def test_immutable_recipe_checkbox_syncs_but_quantity_and_delete_conflict(self):
        a = FakeConnector(Item("recipe", "Mehl", "250 g", editable=False, deletable=False))
        b = FakeConnector(Item("custom", "Mehl", "250 g"))
        await self.run_pair(a, b)
        b.change("custom", completed=True)
        await self.run_pair(a, b)
        self.assertTrue(a.items["recipe"].completed)
        b.change("custom", description="300 g")
        result = await self.run_pair(a, b)
        self.assertFalse(result["ok"])
        self.assertEqual(a.items["recipe"].description, "250 g")
        b.change("custom", description="250 g")
        del b.items["custom"]
        result = await self.run_pair(a, b, propagate_deletions=True)
        self.assertFalse(result["ok"])
        self.assertIn("recipe", a.items)

    async def test_unknown_create_adopts_one_candidate_after_restart_without_retry(self):
        a, b = FakeConnector(Item("a", "Milch", "1 l")), FakeConnector()
        b.fail_add_after_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        self.assertEqual(len(b.items), 1)
        self.restart()
        result = await self.run_pair(a, b)
        self.assertTrue(result["ok"])
        self.assertEqual(len([call for call in b.calls if call[0] == "add"]), 1)
        self.assertEqual(len(b.items), 1)

    async def test_ambiguous_create_candidates_stay_conflicted_without_more_writes(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector()
        b.fail_add_after_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        b.items["duplicate"] = Item("duplicate", "Milch")
        before = list(b.calls)
        self.restart()
        result = await self.run_pair(a, b)
        self.assertFalse(result["ok"])
        self.assertEqual(b.calls, before)
        self.assertEqual(len(b.items), 2)

    async def test_unknown_create_with_no_candidate_never_blindly_retries(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector()
        b.fail_add_after_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        b.items.clear()
        self.assertFalse((await self.run_pair(a, b))["ok"])
        self.assertEqual(len([call for call in b.calls if call[0] == "add"]), 1)

    async def test_pending_create_freezes_target_so_new_item_cannot_be_misattributed(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector()
        b.fail_add_after_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        b.items.clear()
        a.items["a-second"] = Item("a-second", "Milch")
        result = await self.run_pair(a, b)
        self.assertFalse(result["ok"])
        self.assertFalse(b.items)
        self.assertEqual(len([call for call in b.calls if call[0] == "add"]), 1)

    async def test_bring_rename_replacement_uid_is_persisted_and_not_duplicated(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        b.rename_replaces_uid = True
        await self.run_pair(a, b)
        a.change("a", name="Hafermilch")
        await self.run_pair(a, b)
        self.assertNotIn("b", b.items)
        self.assertEqual(b.items["b-renamed"].name, "Hafermilch")
        self.restart()
        result = await self.run_pair(a, b)
        self.assertEqual((result["added"], result["updated"]), (0, 0))

    async def test_partial_updates_eventually_converge_after_restart(self):
        connectors = {name: FakeConnector(Item(name, "Milch")) for name in ("a", "b", "c")}
        targets = [{"provider": name, "list_id": "list"} for name in connectors]
        await self.engine.run(targets, connectors)
        connectors["a"].change("a", completed=True)
        connectors["c"].fail_update_before_write = True
        with self.assertRaises(ConnectorError):
            await self.engine.run(targets, connectors)
        self.assertTrue(connectors["b"].items["b"].completed)
        self.assertFalse(connectors["c"].items["c"].completed)
        self.restart()
        await self.engine.run(targets, connectors)
        self.assertTrue(all(next(iter(connector.items.values())).completed for connector in connectors.values()))

    async def test_uncertain_bring_rename_adopts_replacement_uid_without_duplicates(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        b.rename_replaces_uid = True
        await self.run_pair(a, b)
        a.change("a", name="Hafermilch")
        b.fail_update_after_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        self.assertEqual(b.items["b-renamed"].name, "Hafermilch")
        self.restart()
        result = await self.run_pair(a, b)
        self.assertTrue(result["ok"])
        self.assertEqual((result["added"], result["updated"]), (0, 0))
        self.assertEqual((len(a.items), len(b.items)), (1, 1))

    async def test_edit_during_unconfirmed_update_is_a_conflict_not_an_overwrite(self):
        a, b = FakeConnector(Item("a", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        a.change("a", name="Hafermilch")
        b.fail_update_before_write = True
        with self.assertRaises(ConnectorError):
            await self.run_pair(a, b)
        b.change("b", name="Sojamilch")
        self.restart()
        result = await self.run_pair(a, b)
        self.assertFalse(result["ok"])
        self.assertEqual((a.items["a"].name, b.items["b"].name), ("Hafermilch", "Sojamilch"))

    async def test_account_generation_never_reuses_previous_accounts_item_refs(self):
        a, b = FakeConnector(Item("old-account", "Milch")), FakeConnector(Item("b", "Milch"))
        await self.run_pair(a, b)
        self.store.set("account_generation", 1)
        a.items = {"new-account": Item("new-account", "Eier")}
        result = await self.run_pair(a, b)
        self.assertEqual(result["added"], 2)
        self.assertEqual({item.name for item in a.items.values()}, {"Milch", "Eier"})


if __name__ == "__main__":
    unittest.main()
