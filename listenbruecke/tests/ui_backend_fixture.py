"""Loopback-only UI smoke fixture: real app, temporary store, fake providers.

No vendor authentication or network requests are made by these connectors.
This module is a development test fixture and is not included in the wheel.
"""

import json
import socket
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from listenbruecke.main import create_app
from listenbruecke.models import Item, ShoppingList
from listenbruecke.store import Store


class FakeConnector:
    def __init__(self, provider):
        self.provider = provider
        self.list_id = provider + "-synthetic-list"
        self.sequence = 1
        self.items = [Item(provider + "-1", "Test-Tomaten" if provider == "monsieur_cuisine" else "Testmilch", "500 g" if provider == "monsieur_cuisine" else "1 l")]

    async def list_lists(self):
        return [ShoppingList(self.list_id, "Test-Einkaufsliste")]

    async def test_connection(self):
        return await self.list_lists()

    async def get_items(self, list_id):
        assert list_id == self.list_id
        return list(self.items)

    async def add_item(self, list_id, name, description="", completed=False):
        assert list_id == self.list_id
        self.sequence += 1
        item = Item(f"{self.provider}-{self.sequence}", name, description, completed)
        self.items.append(item)
        return item

    async def update_item(self, list_id, item, **changes):
        assert list_id == self.list_id
        updated = replace(item, **{key: value for key, value in changes.items() if value is not None})
        self.items = [updated if current.uid == item.uid else current for current in self.items]
        return updated

    async def delete_item(self, list_id, item):
        assert list_id == self.list_id
        self.items = [current for current in self.items if current.uid != item.uid]

    async def close(self):
        pass


def run():
    with TemporaryDirectory(prefix="listenbruecke-ui-test-") as directory:
        store = Store(Path(directory))
        store.set_secret("monsieur_cuisine", {"synthetic_test_account": True})
        store.event("info", 'Browserprüfung: <img src=x onerror="window.syntheticXss=true">')
        store.close()
        connectors = {}

        async def factory(provider, credentials):
            assert credentials
            if provider not in connectors:
                connectors[provider] = FakeConnector(provider)
            return connectors[provider]

        app = create_app(directory, background=False, factory=factory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
            config = uvicorn.Config(app, host="127.0.0.1", port=0, access_log=False,
                                    proxy_headers=False, log_level="critical")
            uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    run()
