"""Typed Bring API mocks exercising UUID writes without external requests."""

from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from bring_api.exceptions import BringAuthException

from listenbruecke.connectors import bring as module
from listenbruecke.models import AuthenticationRequired, ConnectorError, Item


class FakeBring:
    instances = []

    def __init__(self, session, email, password):
        self.session = session
        self.login_calls = 0
        self.calls = []
        self.rows = [Item("uuid-a", "Milch", "1 l"), Item("uuid-b", "Milch", "2 l", True)]
        self.instances.append(self)

    async def login(self):
        self.login_calls += 1

    async def load_lists(self):
        return NS(lists=[NS(listUuid="list-a", name="Testliste")])

    async def get_list(self, list_id):
        def typed(i):
            return NS(uuid=i.uid, itemId=i.name, specification=i.description)
        return NS(uuid=list_id, items=NS(purchase=[typed(i) for i in self.rows if not i.completed],
                                        recently=[typed(i) for i in self.rows if i.completed]))

    async def batch_update_list(self, list_id, payload, operation="TO_PURCHASE"):
        self.calls.append((list_id, deepcopy(payload), operation))
        for p in payload if isinstance(payload, list) else [payload]:
            op = p.get("operation", operation)
            self.rows = [i for i in self.rows if i.uid != p["uuid"]]
            if op != "REMOVE":
                self.rows.append(Item(p["uuid"], p["itemId"], p.get("spec", ""), op == "TO_RECENTLY"))


@pytest_asyncio.fixture
async def connector(monkeypatch):
    FakeBring.instances.clear()
    monkeypatch.setattr(module, "Bring", FakeBring)
    instance = module.BringConnector({"email": "test@example.invalid", "password": "synthetic-only"})
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_login_once_persistent_session_and_current_dataclasses(connector):
    assert (await connector.test_connection())[0].id == "list-a"
    first = await connector.get_items("list-a")
    second = await connector.get_items("list-a")
    api = FakeBring.instances[0]
    assert api.login_calls == 1 and len(FakeBring.instances) == 1
    assert api.session is connector._session
    assert [(i.uid, i.name, i.description, i.completed) for i in first] == [
        ("uuid-a", "Milch", "1 l", False), ("uuid-b", "Milch", "2 l", True),
    ]
    assert first == second


@pytest.mark.asyncio
async def test_duplicate_names_never_collapse_or_target_the_neighbour(connector):
    first, second = await connector.get_items("list-a")
    updated = await connector.update_item("list-a", second, completed=False, description="2½ l")
    assert updated.uid == "uuid-b"
    rows = await connector.get_items("list-a")
    assert next(i for i in rows if i.uid == first.uid) == first
    assert len(rows) == 2
    payload = FakeBring.instances[0].calls[0][1]
    assert payload["uuid"] == second.uid
    added = await connector.add_item("list-a", "Milch", "3 l")
    assert added.uid not in {first.uid, second.uid}
    assert len(await connector.get_items("list-a")) == 3


@pytest.mark.asyncio
async def test_rename_replaces_uid_preserves_other_duplicate_and_delete_is_exact(connector):
    first, second = await connector.get_items("list-a")
    renamed = await connector.update_item("list-a", first, name="Haferdrink", completed=True)
    assert renamed.uid != first.uid and renamed.name == "Haferdrink" and renamed.completed
    rows = await connector.get_items("list-a")
    assert first.uid not in {i.uid for i in rows}
    assert next(i for i in rows if i.uid == second.uid) == second
    await connector.delete_item("list-a", second)
    assert [i.uid for i in await connector.get_items("list-a")] == [renamed.uid]


@pytest.mark.asyncio
async def test_unavailable_wrong_list_stale_uid_and_duplicate_ids_fail(connector):
    with pytest.raises(ConnectorError):
        await connector.get_items("different-list")
    with pytest.raises(ConnectorError):
        await connector.delete_item("list-a", Item("unknown-uuid", "Milch"))
    assert not FakeBring.instances[0].calls
    FakeBring.instances[0].rows.append(Item("uuid-a", "Milch", "collision"))
    with pytest.raises(ConnectorError, match="Listenformat"):
        await connector.get_items("list-a")


@pytest.mark.asyncio
async def test_authentication_error_does_not_leak_remote_body(connector, monkeypatch):
    async def fail():
        raise BringAuthException("SYNTHETIC_PRIVATE_RESPONSE")
    await connector.list_lists()
    monkeypatch.setattr(FakeBring.instances[0], "load_lists", fail)
    with pytest.raises(AuthenticationRequired) as exc:
        await connector.list_lists()
    assert "SYNTHETIC_PRIVATE_RESPONSE" not in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_dataclass_response_is_not_empty_list(connector, monkeypatch):
    await connector.list_lists()
    async def invalid(list_id):
        return NS(uuid=list_id, items=None)
    monkeypatch.setattr(FakeBring.instances[0], "get_list", invalid)
    with pytest.raises(ConnectorError):
        await connector.get_items("list-a")
