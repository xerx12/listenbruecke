"""Reviewed provider registration, public setup metadata and credential boundaries.

Adding a provider means registering its descriptor, validators and lazy factory
here, plus implementing the connector protocol. No external code is loaded from
configuration, and unused providers never import their vendor SDK.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from listenbruecke.models import ConnectorError


def _text(value, maximum=16384):
    return isinstance(value, str) and 0 < len(value) <= maximum


def _email(value):
    return _text(value, 320) and "@" in value and not any(char.isspace() for char in value.strip())


def _invalid(message):
    raise ConnectorError(message)


def _bring(value, mode):
    if (set(value) != {"email", "password"} or not _email(value.get("email"))
            or not _text(value.get("password"), 1024)):
        _invalid("Bitte E-Mail und Passwort des Bring!-Kontos eingeben.")
    return {"email": value["email"].strip(), "password": value["password"]}


def _ha(value, mode):
    if value == {"auto": True} and mode == "addon":
        return {"auto": True}
    try:
        if not _text(value.get("url"), 8192):
            raise ValueError
        parsed = urlsplit(value["url"])
        _ = parsed.port
        valid = (set(value) == {"url", "token"} and parsed.scheme in ("http", "https")
                 and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid or not _text(value.get("token"), 8192):
        _invalid("Bitte die Home-Assistant-Adresse und ein Zugriffstoken eingeben.")
    return {"url": value["url"].rstrip("/"), "token": value["token"]}


def _alexa(value, mode):
    def forbidden(data):
        if isinstance(data, dict):
            return any(str(key).casefold() in {"password", "code", "otp", "otp_secret", "mfa_seed", "totp_secret"}
                       or forbidden(item) for key, item in data.items())
        return isinstance(data, list) and any(forbidden(item) for item in data)
    if (set(value) != {"email", "login_data"} or not _email(value.get("email"))
            or not isinstance(value.get("login_data"), dict) or not value["login_data"]
            or forbidden(value["login_data"])):
        _invalid("Die Amazon-Anmeldung konnte nicht sicher gespeichert werden. Bitte erneut anmelden.")
    return json.loads(json.dumps(value, allow_nan=False))


def _mc(value, mode):
    keys = {"jwt", "lidl_refresh_token", "user_id", "companion_app_id"}
    if (set(value) != keys or not all(_text(value.get(key)) for key in keys - {"user_id"})
            or not isinstance(value.get("user_id"), (str, int, type(None))) or isinstance(value["user_id"], bool)):
        _invalid("Die Monsieur-Cuisine-Anmeldung konnte nicht sicher gespeichert werden. Bitte erneut anmelden.")
    return dict(value)


def _google(value, mode):
    if (set(value) != {"email", "master_token"} or not _email(value.get("email"))
            or not _text(value.get("master_token"))):
        _invalid("Bitte Google-E-Mail-Adresse und Master-Token eingeben.")
    return {"email": value["email"].strip(), "master_token": value["master_token"]}


def _cookidoo(value, mode, *, saved=False):
    value = dict(value)
    if not saved:
        value.setdefault("country", "de")
        value.setdefault("language", "de-DE")
    if isinstance(value.get("country"), str):
        value["country"] = value["country"].lower()
    keys = {"email", "country", "language", "auth_data" if saved else "password"}
    valid = (set(value) == keys and _email(value.get("email"))
             and isinstance(value.get("country"), str) and re.fullmatch(r"[a-z]{2}", value["country"])
             and isinstance(value.get("language"), str) and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}", value["language"]))
    if saved:
        auth = value.get("auth_data")
        valid = (valid and isinstance(auth, dict) and set(auth) == {"access_token", "refresh_token", "expires_at"}
                 and _text(auth.get("access_token")) and _text(auth.get("refresh_token"))
                 and type(auth.get("expires_at")) in (int, float) and math.isfinite(auth["expires_at"])
                 and auth["expires_at"] >= 0)
    else:
        valid = valid and _text(value.get("password"), 1024)
    if not valid:
        _invalid("Bitte gültige Cookidoo-Zugangsdaten, Land und Sprache eingeben.")
    return {**deepcopy(value), "email": value["email"].strip()}


def _cookidoo_saved(value, mode):
    return _cookidoo(value, mode, saved=True)


def _field(name, label, type="text", **kwargs):
    return {"name": name, "label": label, "type": type, "required": True, **kwargs}


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    icon: str
    class_name: str
    description: str
    auth_kind: str
    fields: tuple = ()
    hints: tuple[str, ...] = ()
    docs_url: str | None = None
    factory: object = None
    ha_platforms: tuple[str, ...] = ()
    input_validator: object = None
    saved_validator: object = None

    def public(self):
        return {"id": self.id, "name": self.name, "icon": self.icon, "className": self.class_name,
                "description": self.description, "auth_kind": self.auth_kind,
                "fields": deepcopy(list(self.fields)), "hints": list(self.hints), "docs_url": self.docs_url}

    def validate_input(self, value, *, mode="standalone"):
        if not isinstance(value, dict):
            _invalid("Bitte gültige Verbindungsangaben eingeben.")
        if not self.input_validator:
            _invalid("Bitte die Browseranmeldung bei Lidl verwenden." if self.id == "monsieur_cuisine" else
                     "Bitte die Amazon-Anmeldung mit Passwort und aktuellem Einmalcode verwenden.")
        return self.input_validator(value, mode)

    def validate_saved(self, value, *, mode="standalone"):
        if not isinstance(value, dict):
            _invalid("Die Anmeldung konnte nicht sicher gespeichert werden. Bitte erneut anmelden.")
        return (self.saved_validator or self.input_validator)(value, mode)

    def create(self, http, credentials, save):
        return self.factory(http, credentials, save)


def _create_mc(http, credentials, save):
    from listenbruecke.connectors.mc_auth import MCAuth
    from listenbruecke.connectors.monsieur import MonsieurConnector
    return MonsieurConnector(MCAuth(http, credentials, save))


def _create_bring(http, credentials, save):
    from listenbruecke.connectors.bring import BringConnector
    return BringConnector(credentials)


def _create_alexa(http, credentials, save):
    from listenbruecke.connectors.alexa import AlexaConnector
    return AlexaConnector(credentials, save)


def _create_ha(http, credentials, save):
    from listenbruecke.connectors.homeassistant import HomeAssistantConnector
    return HomeAssistantConnector(http, credentials["url"], credentials["token"])


def _create_google(http, credentials, save):
    from listenbruecke.connectors.google_keep import GoogleKeepConnector
    return GoogleKeepConnector(credentials, save)


def _create_cookidoo(http, credentials, save):
    from listenbruecke.connectors.cookidoo import CookidooConnector
    return CookidooConnector(credentials, save)


PROVIDERS = MappingProxyType({spec.id: spec for spec in (
    ProviderSpec("monsieur_cuisine", "Monsieur Cuisine", "pot", "cuisine-icon", "Rezepte und Einkäufe aus deiner Küche.",
                 "browser_code", factory=_create_mc, saved_validator=_mc),
    ProviderSpec("bring", "Bring!", "shopping", "bring-icon", "Gemeinsam planen. Gemeinsam einkaufen.", "credentials",
                 (_field("email", "E-Mail-Adresse", "email", autocomplete="username"),
                  _field("password", "Passwort", "password", autocomplete="current-password")), factory=_create_bring, input_validator=_bring),
    ProviderSpec("alexa", "Amazon Alexa", "alexa", "alexa-icon", "Deine Einkaufsliste per Sprache ergänzen.", "otp",
                 factory=_create_alexa, ha_platforms=("alexa_devices", "alexa_todo_lists"), saved_validator=_alexa),
    ProviderSpec("homeassistant", "Home Assistant", "home", "ha-icon", "Deine Einkaufsliste im smarten Zuhause.",
                 "homeassistant", factory=_create_ha, input_validator=_ha),
    ProviderSpec("google_keep", "Google Keep", "google", "google-icon", "Deine Einkaufslisten aus Google Keep.", "credentials",
                 (_field("email", "Google-E-Mail-Adresse", "email", autocomplete="username"),
                  _field("master_token", "Master-Token", "password", autocomplete="off")),
                 ("Inoffizielle Anbindung.",
                  "Der Master-Token erlaubt weitreichenden Kontozugriff; verwende möglichst ein separates Google-Konto mit geteilter Einkaufsliste.",
                  "Beim Abruf werden technisch alle Keep-Notizen geladen; abgeglichen wird nur die gewählte Liste."),
                 "https://gkeepapi.readthedocs.io/en/latest/#authenticating", factory=_create_google, ha_platforms=("google_keep_sync", "gkeep_sync"), input_validator=_google),
    ProviderSpec("cookidoo", "Cookidoo", "cookidoo", "cookidoo-icon", "Einkäufe und Rezeptzutaten aus deiner Thermomix-Küche.", "credentials",
                 (_field("email", "Cookidoo-E-Mail-Adresse", "email", autocomplete="username"),
                  _field("password", "Passwort", "password", autocomplete="current-password"),
                  _field("country", "Land (z. B. de)", default="de", placeholder="de"),
                  _field("language", "Sprache (z. B. de-DE)", default="de-DE", placeholder="de-DE")),
                 ("Inoffizielle Anbindung. Rezeptzutaten können abgehakt werden; frei ergänzte Einkäufe lassen sich vollständig bearbeiten.",),
                 factory=_create_cookidoo, ha_platforms=("cookidoo",), input_validator=_cookidoo, saved_validator=_cookidoo_saved),
)})
