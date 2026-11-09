"""Actual HA Core with a synthetic, loopback-free local application API."""

from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.listenbruecke.const import CONF_TOKEN, CONF_URL, DOMAIN

BASE = "http://listenbruecke.test:8099"
TOKEN = "synthetic-dedicated-app-token"
INSTANCE = "synthetic-instance-123"


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    """Allow just the custom integrations visible in this test repository."""
    yield


class FakeLocalApi:
    def __init__(self):
        self.state = {"instance_id": INSTANCE, "name": "Listenbrücke", "version": "0.2.0",
                      "sync": {"enabled": True, "interval_seconds": 60, "running": False,
                               "last_success": "2026-11-09T18:30:00+00:00", "last_error": None, "items_synced": 42},
                      "providers": [{"id": provider, "name": provider, "configured": True,
                                     "connected": True, "last_test": None, "error": None}
                                    for provider in ("bring", "monsieur_cuisine", "alexa", "homeassistant")]}
        self.items = [{"uid": "item-a", "summary": "Milch", "description": "1 l", "status": "needs_action"},
                      {"uid": "item-b", "summary": "Milch", "description": "2 l", "status": "completed"}]
        self.available = True
        self.status = 200
        self.write_status = 200
        self.fail_paths = set()
        self.calls = []

    async def callback(self, method, url, data):
        path = url.path.removeprefix("/api/ha/v1/")
        self.calls.append((method, path, deepcopy(data)))
        if method == "GET":
            payload = self.state if path == "state" else {"available": self.available, "items": self.items}
            return AiohttpClientMockResponse(method, url, status=self.status, json=deepcopy(payload))
        if self.write_status != 200 or path in self.fail_paths:
            return AiohttpClientMockResponse(method, url, status=self.write_status if self.write_status != 200 else 503, json={"secret": "synthetic-private-response"})
        if path == "items" and method == "POST":
            assert data["request_id"]
            item = {"uid": "item-c", "summary": data["summary"], "description": data.get("description", ""), "status": "needs_action"}
            self.items.append(item)
            return AiohttpClientMockResponse(method, url, json={"item": item})
        if path.startswith("items/"):
            uid = path.removeprefix("items/")
            item = next(item for item in self.items if item["uid"] == uid)
            if method == "PATCH":
                item.update(data)
                return AiohttpClientMockResponse(method, url, json={"item": item})
            assert method == "DELETE" and data == {}
            self.items.remove(item)
            return AiohttpClientMockResponse(method, url, json={"ok": True})
        if path == "settings":
            self.state["sync"].update(data)
            return AiohttpClientMockResponse(method, url, json={"ok": True})
        assert path == "sync" and method == "POST" and data == {}
        return AiohttpClientMockResponse(method, url, status=202, json={"ok": True})


@pytest.fixture
def local_api(aioclient_mock):
    api = FakeLocalApi()
    for method, path in (("get", "state"), ("get", "items"), ("post", "items"),
                         ("patch", "items/item-a"), ("patch", "items/item-b"),
                         ("delete", "items/item-a"), ("delete", "items/item-b"),
                         ("put", "settings"), ("post", "sync")):
        getattr(aioclient_mock, method)(BASE + "/api/ha/v1/" + path, side_effect=api.callback)
    yield api
    for _, _, _, headers in aioclient_mock.mock_calls:
        assert headers["Authorization"] == "Bearer " + TOKEN


@pytest.fixture
def config_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Listenbrücke", unique_id=INSTANCE,
                            data={CONF_URL: BASE, CONF_TOKEN: TOKEN})
    entry.add_to_hass(hass)
    return entry
