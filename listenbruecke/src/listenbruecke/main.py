"""Authenticated web UI and background worker for a self-hosted installation."""

import asyncio
import base64
import binascii
import contextlib
import io
import ipaddress
import json
import logging
import os
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from listenbruecke import __version__
from listenbruecke.backup import BackupError, IMPORT_JSON_BYTES, apply_pending_import, backup_summary, stage_import
from listenbruecke.bridge import (TARGET as BRIDGE_TARGET, BridgeConnector, integration_config,
                                 integration_status, issue_token, machine_item, valid_token)
from listenbruecke.models import ConnectorError
from listenbruecke.providers import PROVIDERS as PROVIDER_REGISTRY
from listenbruecke.store import Store, now
from listenbruecke.sync import SyncEngine

PROVIDERS = {provider: spec.name for provider, spec in PROVIDER_REGISTRY.items()}
DEFAULT_SETTINGS = {"enabled": False, "interval_seconds": 60, "targets": [], "propagate_deletions": False}
COOKIE = "listenbruecke_session"


def asset_directory(name):
    package = Path(__file__).resolve().parent
    installed = package / name
    return installed if installed.is_dir() else package.parent.parent / name


class Service:
    def __init__(self, store, *, factory=None):
        self.store = store
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
        self.clients = {}
        self.factory = factory
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.manual_task = None
        self.alexa_auth_task = None
        self.alexa_auth_status = {"running": False, "complete": False, "error": None}
        self._sync_running = False
        self.restore_pending = False
        self.mode = "addon" if os.environ.get("SUPERVISOR_TOKEN") else "standalone"
        self.provider_status = {}
        for provider in PROVIDERS:
            saved = self.store.get("provider_status:" + provider, {})
            self.provider_status[provider] = {**saved, "connected": False, "error": None}

    def settings(self):
        return {**DEFAULT_SETTINGS, **self.store.get("sync_settings", {})}

    def credentials(self, provider):
        if provider == "homeassistant" and self.mode == "addon":
            return {"url": "http://supervisor/core", "token": os.environ["SUPERVISOR_TOKEN"], "auto": True}
        return self.store.get_secret(provider)

    def provider(self, provider):
        data = self.provider_status[provider]
        return {"id": provider, "name": PROVIDERS[provider], "configured": bool(self.credentials(provider)),
                "selected": any(target["provider"] == provider for target in self.settings()["targets"]),
                "connected": data.get("connected", False), "error": data.get("error"),
                "lists": data.get("lists", []), "last_test": data.get("last_test")}

    def status(self):
        metrics = self.store.get("sync_metrics", {})
        return {"version": __version__, "mode": self.mode,
                "providers": [self.provider(p) for p in PROVIDERS],
                "sync": {**self.settings(), "last_run": None, "last_success": None,
                         "last_error": None, "items_synced": 0, **metrics,
                         "running": self._sync_running or bool(self.manual_task and not self.manual_task.done())},
                "events": self.store.events(12)}

    async def connector(self, provider):
        if provider in self.clients:
            return self.clients[provider]
        if provider == "_bridge":
            client = BridgeConnector(self.store)
            self.clients[provider] = client
            return client
        credentials = self.credentials(provider)
        if not credentials:
            raise ConnectorError("Bitte zuerst die Verbindung einrichten.")
        if self.factory:
            client = await self.factory(provider, credentials)
        else:
            spec = PROVIDER_REGISTRY.get(provider)
            if spec is None:
                raise ConnectorError("Diese Verbindung ist nicht bekannt.")
            async def save(value):
                self.store.set_secret(provider, spec.validate_saved(value, mode=self.mode))
            client = spec.create(self.http, credentials, save)
        self.clients[provider] = client
        return client

    async def forget_client(self, provider):
        client = self.clients.pop(provider, None)
        if client:
            await client.close()
        self.provider_status[provider] = {"connected": False, "lists": [], "error": None, "last_test": None}
        self.store.set("provider_status:" + provider, self.provider_status[provider])

    def disable_for_change(self, provider):
        # Unrelated optional accounts must not discard the running group's
        # durable write intentions. A later selection still uses fresh IDs.
        settings = self.settings()
        selected = any(target["provider"] == provider for target in settings["targets"])
        if selected:
            # Persist the pause before identity/credentials change, including
            # when the process stops between individual SQLite commits.
            settings["enabled"] = False
            self.store.set("sync_settings", settings)
        versions = self.store.get("provider_account_generations", {})
        versions[provider] = versions.get(provider, 0) + 1
        self.store.set("provider_account_generations", versions)
        if selected:
            self.store.set("account_generation", self.store.get("account_generation", 0) + 1)

    @staticmethod
    def validate_alexa_credentials(value):
        return PROVIDER_REGISTRY["alexa"].validate_saved(value)

    def safe_error(self, error, *, provider=None):
        if provider == "alexa":
            return "Die Alexa-Verbindung konnte nicht geprüft werden. Bitte die Amazon-Anmeldung erneut durchführen."
        # SDK responses can contain nested cookies/tokens. Inspect every saved
        # string, including future providers, without logging the response.
        message = str(error)
        def contains_secret(value):
            if isinstance(value, dict):
                return any(contains_secret(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_secret(item) for item in value)
            return isinstance(value, str) and len(value) >= 4 and value in message
        if any(contains_secret(self.credentials(name) or {}) for name in PROVIDERS):
            return "Die Verbindung konnte nicht geprüft werden. Bitte Einstellungen und Erreichbarkeit prüfen."
        return message

    async def test(self, provider):
        try:
            client = await self.connector(provider)
            lists = await asyncio.wait_for(client.test_connection(), timeout=45)
            status = {"connected": True, "error": None, "lists": [x.to_dict() for x in lists], "last_test": now()}
            self.store.event("success", PROVIDERS[provider] + ": Verbindung erfolgreich geprüft.")
        except ConnectorError as exc:
            status = {"connected": False, "error": self.safe_error(exc, provider=provider), "lists": [], "last_test": now()}
            self.store.event("error", PROVIDERS[provider] + ": Verbindungstest fehlgeschlagen.")
        except Exception:
            status = {"connected": False, "error": "Die Verbindung konnte nicht geprüft werden. Bitte Einstellungen und Erreichbarkeit prüfen.", "lists": [], "last_test": now()}
            self.store.event("error", PROVIDERS[provider] + ": Verbindungstest fehlgeschlagen.")
        self.provider_status[provider] = status
        self.store.set("provider_status:" + provider, status)
        return self.provider(provider)

    async def synchronize(self):
        if self.lock.locked() or self.alexa_auth_running():
            raise ConnectorError("Ein Verbindungs- oder Synchronisierungsvorgang läuft bereits.")
        async with self.lock:
            self._sync_running = True
            settings = self.settings()
            metrics = self.store.get("sync_metrics", {})
            metrics.update({"last_run": now(), "last_error": None})
            self.store.set("sync_metrics", metrics)
            try:
                targets = list(settings["targets"])
                if self.store.get("ha_integration", {}).get("enabled"):
                    targets.append(dict(BRIDGE_TARGET))
                clients = {t["provider"]: await self.connector(t["provider"]) for t in targets}
                await self.validate_target_mirrors(settings["targets"], clients)
                result = await SyncEngine(self.store).run(targets, clients,
                    propagate_deletions=settings["propagate_deletions"])
                # A complete reconciliation also verifies every selected source.
                # Refresh restart-reset connection flags without discarding the
                # list metadata collected by the explicit connection test.
                for provider in clients:
                    if provider not in PROVIDERS:
                        continue
                    status = {**self.provider_status[provider], "connected": True,
                              "error": None, "last_test": now()}
                    self.provider_status[provider] = status
                    self.store.set("provider_status:" + provider, status)
                metrics["items_synced"] = metrics.get("items_synced", 0) + result["added"] + result["updated"]
                if result["ok"]:
                    metrics["last_success"] = now()
                else:
                    metrics["last_error"] = result["message"]
                self.store.event("success" if result["ok"] else "warning", result["message"])
                return result
            except ConnectorError as exc:
                metrics["last_error"] = self.safe_error(exc)
                self.store.event("error", "Abgleich angehalten: " + metrics["last_error"])
                raise ConnectorError(metrics["last_error"]) from None
            except Exception:
                metrics["last_error"] = "Der Abgleich konnte nicht abgeschlossen werden. Die Listen wurden nicht als leer behandelt."
                self.store.event("error", metrics["last_error"])
                raise ConnectorError(metrics["last_error"]) from None
            finally:
                self._sync_running = False
                self.store.set("sync_metrics", metrics)

    def alexa_auth_running(self):
        return bool(self.alexa_auth_task and not self.alexa_auth_task.done())

    def sync_running(self):
        return self._sync_running or bool(self.manual_task and not self.manual_task.done()) or self.alexa_auth_running()

    async def validate_target_mirrors(self, targets, clients=None):
        selected = {target["provider"] for target in targets}
        if "homeassistant" not in selected or not any(
                PROVIDER_REGISTRY[provider].ha_platforms for provider in selected):
            return
        client = clients["homeassistant"] if clients is not None else await self.connector("homeassistant")
        check = getattr(client, "check_independent_source", None)
        if check is None:
            raise ConnectorError("Die Herkunft der Home-Assistant-Liste konnte nicht geprüft werden. Bitte die Verbindung erneut testen.")
        target = next(target for target in targets if target["provider"] == "homeassistant")
        await check(target["list_id"], selected)

    def start_alexa_login(self, email, password, code):
        if self.restore_pending:
            raise HTTPException(409, "Die Wiederherstellung wartet auf den Neustart der App.")
        if self.lock.locked() or self.sync_running():
            raise HTTPException(409, "Ein Vorgang läuft bereits. Bitte anschließend erneut anmelden.")
        self.alexa_auth_status = {"running": True, "complete": False, "error": None}
        transient = {"email": email, "password": password, "code": code}

        async def perform():
            try:
                async with self.lock:
                    from listenbruecke.connectors.alexa import AlexaConnector
                    value = await asyncio.wait_for(AlexaConnector.login(**transient), timeout=90)
                    credentials = self.validate_alexa_credentials(value)
                    transient.clear()
                    self.disable_for_change("alexa")
                    await self.forget_client("alexa")
                    self.store.set_secret("alexa", credentials)
                    result = await self.test("alexa")
                    if not result["connected"]:
                        self.alexa_auth_status["error"] = result["error"]
                    self.alexa_auth_status["complete"] = result["connected"]
            except asyncio.CancelledError:
                self.alexa_auth_status["error"] = "Die Amazon-Anmeldung wurde abgebrochen. Bitte erneut starten."
                raise
            except Exception:
                self.alexa_auth_status["error"] = "Die Amazon-Anmeldung ist fehlgeschlagen oder abgelaufen. Bitte Passwort und aktuellen Einmalcode prüfen und erneut versuchen."
                self.store.event("error", "Alexa: Amazon-Anmeldung fehlgeschlagen.")
            finally:
                transient.clear()
                self.alexa_auth_status["running"] = False
                self.wakeup.set()

        self.alexa_auth_task = asyncio.create_task(perform())

    @asynccontextmanager
    async def configure(self):
        if self.restore_pending:
            raise HTTPException(409, "Die Wiederherstellung wartet auf den Neustart der App.")
        if self.lock.locked() or self.sync_running():
            raise HTTPException(409, "Ein Abgleich oder Verbindungstest läuft. Bitte danach erneut versuchen.")
        async with self.lock:
            yield

    def start_sync(self):
        if self.restore_pending:
            raise ConnectorError("Die Wiederherstellung wartet auf den Neustart der App.")
        if self.lock.locked() or self.sync_running():
            raise ConnectorError("Ein Verbindungs- oder Synchronisierungsvorgang läuft bereits.")
        async def perform():
            try:
                await self.synchronize()
            except ConnectorError:
                pass  # synchronize has already persisted the sanitized failure.
        self.manual_task = asyncio.create_task(perform())

    async def worker(self):
        while True:
            self.wakeup.clear()
            settings = self.settings()
            if settings["enabled"] and not self.sync_running() and not self.restore_pending:
                try:
                    await self.synchronize()
                except ConnectorError:
                    pass
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=settings["interval_seconds"])
            except TimeoutError:
                pass

    async def close(self):
        if self.alexa_auth_task and not self.alexa_auth_task.done():
            self.alexa_auth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.alexa_auth_task
        if self.manual_task and not self.manual_task.done():
            self.manual_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.manual_task
        for client in self.clients.values():
            await client.close()
        await self.http.aclose()
        self.store.close()


def create_app(data_dir=None, *, factory=None, background=True):
    directory = Path(data_dir or os.environ.get("LISTENBRUECKE_DATA", "/data" if os.environ.get("SUPERVISOR_TOKEN") else "data"))
    attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        apply_pending_import(directory)
        service = Service(Store(directory), factory=factory)
        app.state.service = service
        task = asyncio.create_task(service.worker()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await service.close()

    app = FastAPI(title="Listenbrücke", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def ingress(request):
        # The add-on exposes no network port. Only Supervisor's ingress proxy is trusted.
        return (bool(os.environ.get("SUPERVISOR_TOKEN")) and request.client is not None
                and request.client.host == "172.30.32.2"
                and request.headers.get("x-ingress-path", "").startswith("/api/hassio_ingress/"))

    def trusted_host(request):
        if ingress(request):
            return True
        try:
            parsed = urlsplit("//" + request.headers.get("host", ""))
            host = parsed.hostname
            _ = parsed.port
            if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
            allowed = {"localhost", "homeassistant", "homeassistant.local"}
            allowed.update(value.strip().lower() for value in os.environ.get("LISTENBRUECKE_ALLOWED_HOSTS", "").split(",") if value.strip())
            if host.lower() in allowed:
                return True
            address = ipaddress.ip_address(host)
            return address.is_loopback or address.is_private
        except ValueError:
            return False

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            machine_path = request.url.path.startswith("/api/ha/v1/")
            machine_auth = machine_path and valid_token(app.state.service.store, request.headers.get("authorization"))
            if machine_path and not machine_auth:
                return JSONResponse({"detail": "Der Zugriff der Home-Assistant-Integration ist ungültig."}, status_code=401)
            if not machine_auth and not trusted_host(request):
                return JSONResponse({"detail": "Diese Serveradresse ist nicht freigegeben."}, status_code=403)
            body_limit = IMPORT_JSON_BYTES if request.url.path == "/api/backup/import" else 32768
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return JSONResponse({"detail": "Die Anfrage muss JSON enthalten."}, status_code=415)
                try:
                    length = int(request.headers.get("content-length", "0"))
                except ValueError:
                    return JSONResponse({"detail": "Ungültige Anfrage."}, status_code=400)
                if length > body_limit:
                    return JSONResponse({"detail": "Die Anfrage ist zu groß."}, status_code=413)
                origin = request.headers.get("origin")
                if origin and not ingress(request) and not machine_auth:
                    try:
                        origin_url = urlsplit(origin)
                        valid_origin = (origin_url.netloc == request.headers.get("host")
                                        and origin_url.scheme in ("http", "https")
                                        and not origin_url.path and not origin_url.query and not origin_url.fragment)
                    except ValueError:
                        valid_origin = False
                    if not valid_origin:
                        return JSONResponse({"detail": "Die Anfrage stammt nicht aus dieser Oberfläche."}, status_code=403)
            public = request.url.path in ("/api/session", "/api/setup", "/api/login")
            if not public and not machine_auth and not ingress(request) and not app.state.service.store.valid_session(request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "Bitte zuerst anmelden."}, status_code=401)
            if app.state.service.restore_pending and request.method in ("POST", "PUT", "PATCH", "DELETE"):
                return JSONResponse({"detail": "Die Wiederherstellung wartet auf den Neustart der App."}, status_code=409)
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > body_limit:
                        return JSONResponse({"detail": "Die Anfrage ist zu groß."}, status_code=413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": "Bitte die eingegebenen Angaben prüfen."}, status_code=422)

    @app.exception_handler(ConnectorError)
    async def connector_error(request, exc):
        return JSONResponse({"detail": app.state.service.safe_error(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({"detail": "Die Anfrage konnte nicht abgeschlossen werden."}, status_code=500)

    def set_session(response, request, token):
        path = request.headers.get("x-ingress-path", "/") if ingress(request) else "/"
        response.set_cookie(COOKIE, token, max_age=86400, httponly=True, samesite="lax", path=path,
                            secure=os.environ.get("LISTENBRUECKE_SECURE_COOKIE") == "1" or request.url.scheme == "https")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/session")
    async def session(request: Request):
        store = app.state.service.store
        return {"authenticated": ingress(request) or store.valid_session(request.cookies.get(COOKIE)),
                "setup_required": store.setup_required() and not ingress(request), "ingress": ingress(request)}

    @app.post("/api/setup")
    async def setup(request: Request, data: dict):
        store = app.state.service.store
        if not store.setup_required():
            raise HTTPException(409, "Die Oberfläche ist bereits eingerichtet.")
        password = data.get("password")
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise HTTPException(400, "Bitte ein Passwort mit mindestens 12 Zeichen wählen.")
        store.set_password(password)
        response = JSONResponse({"ok": True})
        set_session(response, request, store.new_session())
        return response

    @app.post("/api/login")
    async def login(request: Request, data: dict):
        peer = request.client.host if request.client else "unknown"
        recent = [stamp for stamp in attempts.get(peer, []) if time.monotonic() - stamp < 300]
        if len(recent) >= 5:
            raise HTTPException(429, "Bitte einige Minuten warten und die Anmeldung erneut versuchen.")
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 1024 or not app.state.service.store.check_password(password):
            attempts[peer] = recent + [time.monotonic()]
            raise HTTPException(401, "Das Passwort stimmt nicht.")
        attempts.pop(peer, None)
        response = JSONResponse({"ok": True})
        set_session(response, request, app.state.service.store.new_session())
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        app.state.service.store.delete_session(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/status")
    async def status():
        return app.state.service.status()

    @app.get("/api/integration")
    async def get_integration():
        service = app.state.service
        return integration_status(service.store, addon=service.mode == "addon")

    @app.post("/api/integration/token")
    async def create_integration_token(data: dict):
        if data:
            raise HTTPException(400, "Diese Anfrage benötigt keine zusätzlichen Angaben.")
        service = app.state.service
        async with service.configure():
            if not service.store.get("ha_integration", {}).get("enabled"):
                SyncEngine(service.store).include_target(service.settings()["targets"], BRIDGE_TARGET)
            token = issue_token(service.store)
            service.wakeup.set()
            result = integration_status(service.store, addon=service.mode == "addon")
            return {"token": token, "instance_id": result["instance_id"], "suggested_url": result["suggested_url"]}

    def seen_by_ha():
        service = app.state.service
        config = integration_config(service.store)
        config["last_seen"] = now()
        service.store.set("ha_integration", config)
        return service, config

    @app.get("/api/ha/v1/state")
    async def ha_state():
        service, config = seen_by_ha()
        status = service.status()
        return {"instance_id": config["instance_id"], "name": "Listenbrücke", "version": __version__,
                "sync": {key: status["sync"][key] for key in
                         ("enabled", "interval_seconds", "running", "last_success", "last_error", "items_synced")},
                "providers": [{key: provider[key] for key in
                               ("id", "name", "configured", "selected", "connected", "last_test", "error")}
                              for provider in status["providers"]]}

    @app.get("/api/ha/v1/items")
    async def ha_items():
        service, _ = seen_by_ha()
        connector = await service.connector("_bridge")
        return {"available": not service.restore_pending,
                "items": [machine_item(item) for item in await connector.get_items("local")]}

    async def bridge_item(uid):
        connector = await app.state.service.connector("_bridge")
        item = next((item for item in await connector.get_items("local") if item.uid == uid), None)
        if item is None:
            raise HTTPException(404, "Der Artikel ist nicht mehr verfügbar.")
        return connector, item

    @app.post("/api/ha/v1/items")
    async def ha_add_item(data: dict):
        if not set(data) <= {"summary", "description", "request_id"} or "summary" not in data:
            raise HTTPException(400, "Bitte einen Artikelnamen übergeben.")
        service, _ = seen_by_ha()
        async with service.configure():
            connector = await service.connector("_bridge")
            item = await connector.add_item("local", data["summary"], data.get("description", ""),
                                             request_id=data.get("request_id"))
            service.wakeup.set()
        return {"item": machine_item(item)}

    @app.patch("/api/ha/v1/items/{uid}")
    async def ha_update_item(uid: str, data: dict):
        if not data or not set(data) <= {"summary", "description", "status"}:
            raise HTTPException(400, "Bitte gültige Artikelangaben übergeben.")
        values = {}
        for field, value in data.items():
            if field == "status":
                if value not in ("needs_action", "completed"):
                    raise HTTPException(400, "Bitte einen gültigen Artikelstatus übergeben.")
                values["completed"] = value == "completed"
            else:
                values["name" if field == "summary" else field] = value
        service, _ = seen_by_ha()
        async with service.configure():
            connector, item = await bridge_item(uid)
            item = await connector.update_item("local", item, **values)
            service.wakeup.set()
        return {"item": machine_item(item)}

    @app.delete("/api/ha/v1/items/{uid}")
    async def ha_delete_item(uid: str):
        service, _ = seen_by_ha()
        async with service.configure():
            connector, item = await bridge_item(uid)
            await connector.delete_item("local", item)
            service.wakeup.set()
        return {"ok": True}

    @app.put("/api/ha/v1/settings")
    async def ha_settings(data: dict):
        if not data or not set(data) <= {"enabled", "interval_seconds"}:
            raise HTTPException(400, "Bitte eine gültige Synchronisierungseinstellung übergeben.")
        if ("enabled" in data and type(data["enabled"]) is not bool
                or "interval_seconds" in data and
                (type(data["interval_seconds"]) is not int or not 30 <= data["interval_seconds"] <= 3600)):
            raise HTTPException(400, "Bitte ein Intervall zwischen 30 und 3600 Sekunden wählen.")
        service, _ = seen_by_ha()
        async with service.configure():
            settings = {**service.settings(), **data}
            if settings["enabled"] and not settings["targets"]:
                raise HTTPException(400, "Bitte zuerst mindestens eine externe Liste auswählen.")
            service.store.set("sync_settings", settings)
            service.wakeup.set()
        return {"ok": True, "enabled": settings["enabled"], "interval_seconds": settings["interval_seconds"]}

    @app.post("/api/ha/v1/sync")
    async def ha_sync(data: dict):
        if data:
            raise HTTPException(400, "Diese Anfrage benötigt keine zusätzlichen Angaben.")
        service, _ = seen_by_ha()
        service.start_sync()
        return JSONResponse({"ok": True, "accepted": True}, status_code=202)

    @app.get("/api/backup/summary")
    async def backup_state_summary():
        service = app.state.service
        if service.lock.locked():
            raise HTTPException(409, "Ein Vorgang läuft bereits. Bitte anschließend erneut versuchen.")
        async with service.lock:
            try:
                return backup_summary(directory)
            except BackupError as exc:
                raise HTTPException(400, str(exc)) from None

    @app.post("/api/backup/import")
    async def import_backup(data: dict):
        if set(data) != {"archive"} or not isinstance(data.get("archive"), str):
            raise HTTPException(400, "Bitte eine gültige Sicherung übergeben.")
        try:
            archive = base64.b64decode(data["archive"], validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "Die Sicherung ist ungültig.") from None
        service = app.state.service
        if service.lock.locked() or service.sync_running():
            raise HTTPException(409, "Ein Vorgang läuft bereits. Bitte anschließend erneut versuchen.")
        async with service.lock:
            try:
                summary = stage_import(directory, archive)
            except BackupError as exc:
                raise HTTPException(400, str(exc)) from None
            service.restore_pending = True
            service.wakeup.set()
        return {"ok": True, "requires_restart": True, "summary": summary}

    @app.get("/api/providers")
    async def providers():
        return {"providers": [spec.public() for spec in PROVIDER_REGISTRY.values()]}

    @app.get("/api/events")
    async def events():
        return app.state.service.store.events()

    def check_provider(provider):
        if provider not in PROVIDERS:
            raise HTTPException(404, "Diese Verbindung ist nicht bekannt.")

    @app.put("/api/connections/{provider}")
    async def save_connection(provider: str, data: dict):
        check_provider(provider)
        service = app.state.service
        credentials = PROVIDER_REGISTRY[provider].validate_input(data, mode=service.mode)
        async with service.configure():
            service.disable_for_change(provider)
            await service.forget_client(provider)
            service.store.set_secret(provider, credentials)
            service.store.event("info", PROVIDERS[provider] + ": Einstellungen gespeichert. Bitte Verbindung testen.")
        return service.provider(provider)

    @app.delete("/api/connections/{provider}")
    async def delete_connection(provider: str):
        check_provider(provider)
        service = app.state.service
        async with service.configure():
            service.disable_for_change(provider)
            await service.forget_client(provider)
            service.store.delete_secret(provider)
            if provider == "alexa":
                service.alexa_auth_status = {"running": False, "complete": False, "error": None}
        return service.provider(provider)

    @app.post("/api/connections/{provider}/test")
    async def test_connection(provider: str):
        check_provider(provider)
        async with app.state.service.configure():
            return await app.state.service.test(provider)

    @app.post("/api/connections/monsieur_cuisine/auth/start")
    async def start_mc_login():
        from listenbruecke.connectors.mc_auth import MCAuth
        flow = MCAuth.start_login()
        app.state.service.store.set_secret("oauth:" + flow["state"], {"verifier": flow["verifier"], "expires": time.time() + 600})
        return {"authorization_url": flow["authorization_url"]}

    @app.post("/api/connections/alexa/auth/start")
    async def start_alexa_login(data: dict):
        if (set(data) != {"email", "password", "code"}
                or not isinstance(data.get("email"), str) or "@" not in data["email"] or len(data["email"]) > 320
                or not isinstance(data.get("password"), str) or not 1 <= len(data["password"]) <= 1024
                or not isinstance(data.get("code"), str) or len(data["code"].strip()) != 6
                or not data["code"].strip().isascii() or not data["code"].strip().isdigit()):
            raise HTTPException(400, "Bitte E-Mail, Passwort und den aktuellen sechsstelligen Amazon-Einmalcode eingeben.")
        app.state.service.start_alexa_login(data["email"].strip(), data["password"], data["code"].strip())
        return JSONResponse({"running": True}, status_code=202)

    @app.get("/api/connections/alexa/auth/status")
    async def alexa_login_status():
        return dict(app.state.service.alexa_auth_status)

    @app.post("/api/connections/monsieur_cuisine/auth/complete")
    async def complete_mc_login(data: dict):
        from listenbruecke.connectors.mc_auth import MCAuth
        code, state = data.get("code"), data.get("state")
        if not isinstance(code, str) or not isinstance(state, str) or not 1 <= len(code) <= 4096 or not 1 <= len(state) <= 1024:
            raise HTTPException(400, "Bitte das vollständige Anmeldeergebnis aus dem Browserhelfer einfügen.")
        service = app.state.service
        completed = {}
        async def save(credentials):
            completed.update(PROVIDER_REGISTRY["monsieur_cuisine"].validate_saved(credentials))
        async with service.configure():
            pending = service.store.consume_secret("oauth:" + state)
            if not pending or pending["expires"] < time.time():
                raise HTTPException(400, "Die Anmeldung ist abgelaufen oder wurde schon verwendet. Bitte neu beginnen.")
            auth = MCAuth(service.http, {}, save)
            await auth.complete_login(code, pending["verifier"])
            credentials = PROVIDER_REGISTRY["monsieur_cuisine"].validate_saved(completed)
            service.disable_for_change("monsieur_cuisine")
            await service.forget_client("monsieur_cuisine")
            service.store.set_secret("monsieur_cuisine", credentials)
            return await service.test("monsieur_cuisine")

    @app.get("/api/browser-helper.zip")
    async def browser_helper():
        output = io.BytesIO()
        helper = asset_directory("browser-helper")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(helper.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(helper))
            notice = helper.parent / "THIRD_PARTY_LICENSES.md"
            if notice.is_file():
                archive.write(notice, "THIRD_PARTY_LICENSES.md")
        return Response(output.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="listenbruecke-browserhelfer.zip"'})

    @app.get("/api/settings")
    async def get_settings():
        return app.state.service.settings()

    @app.put("/api/settings")
    async def save_settings(data: dict):
        if not set(data) <= set(DEFAULT_SETTINGS):
            raise HTTPException(400, "Unbekannte Einstellung.")
        settings = {**DEFAULT_SETTINGS, **data}
        if (type(settings["enabled"]) is not bool or type(settings["propagate_deletions"]) is not bool
                or type(settings["interval_seconds"]) is not int or not 30 <= settings["interval_seconds"] <= 3600
                or not isinstance(settings["targets"], list) or len(settings["targets"]) > len(PROVIDERS)):
            raise HTTPException(400, "Bitte ein Intervall zwischen 30 und 3600 Sekunden und gültige Listen auswählen.")
        providers = set()
        service = app.state.service
        async with service.configure():
            for target in settings["targets"]:
                if (not isinstance(target, dict) or set(target) != {"provider", "list_id"}
                        or not isinstance(target["provider"], str)
                        or target["provider"] not in PROVIDERS or not isinstance(target["list_id"], str)
                        or target["provider"] in providers):
                    raise HTTPException(400, "Bitte pro Anbieter höchstens eine Liste auswählen.")
                provider = target["provider"]
                providers.add(provider)
                if not service.provider(provider)["connected"]:
                    raise HTTPException(400, PROVIDERS[provider] + ": Bitte zuerst die Verbindung erfolgreich testen.")
                if target["list_id"] not in {x["id"] for x in service.provider(provider)["lists"]}:
                    raise HTTPException(400, "Die ausgewählte Liste ist nicht verfügbar oder bereits über einen anderen Anbieter eingebunden.")
            total_targets = len(settings["targets"]) + bool(service.store.get("ha_integration", {}).get("enabled"))
            if settings["enabled"] and total_targets < 2:
                raise HTTPException(400, "Für den automatischen Abgleich mindestens zwei Listen auswählen.")
            await service.validate_target_mirrors(settings["targets"])
            service.store.set("sync_settings", settings)
            service.store.event("info", "Automatischer Abgleich aktiviert." if settings["enabled"] else "Einstellungen gespeichert; automatischer Abgleich pausiert.")
            service.wakeup.set()
        return settings

    @app.post("/api/sync")
    async def sync_now():
        app.state.service.start_sync()
        return JSONResponse({"ok": True, "accepted": True, "added": 0, "updated": 0,
            "removed": 0, "conflicts": 0,
            "message": "Der Abgleich wurde gestartet. Den Fortschritt findest du in der Übersicht."}, status_code=202)

    web = asset_directory("web")
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def run():
    import uvicorn
    logging.getLogger("bring_api").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Provider exception messages or query strings must never enter access logs.
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    uvicorn.run(create_app(), host=os.environ.get("LISTENBRUECKE_HOST", "0.0.0.0"),
                port=int(os.environ.get("LISTENBRUECKE_PORT", "8099")), access_log=False,
                proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    run()
