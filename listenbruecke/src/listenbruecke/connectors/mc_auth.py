"""Monsieur Cuisine browser login and durable token renewal.

The user signs in on Lidl's website; this module never accepts their password.
PKCE/token exchange and crypto are adapted from MrVain74/ha-monsieur-cuisine
(MIT, Copyright 2026 MrVain74). See THIRD_PARTY_LICENSES.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from listenbruecke.models import AuthenticationRequired, ConnectorError

API_BASE = "https://mc-api.tecpal.com"
ACCOUNTS = "https://accounts.lidl.com"
CLIENT_ID = "monsieurcuisineappcompanionclient"
REDIRECT_URI = "lidl-com.monsieur-cuisine.Companion://login/prod"
SCOPE = "openid profile offline_access"
KEY_ID = "351756051523999"


def _encrypt_login_payload(plaintext: bytes, public_key_b64: str) -> tuple[str, str, str, bytes]:
    key, iv = secrets.token_bytes(32), secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    public_key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
    wrapped_key = public_key.encrypt(key, rsa_padding.PKCS1v15())
    return tuple(base64.b64encode(value).decode("ascii")
                 for value in (ciphertext, iv, wrapped_key)) + (key,)


def _decrypt_login_response(payload_b64: str, iv_b64: str, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(base64.b64decode(iv_b64))).decryptor()
    padded = decryptor.update(base64.b64decode(payload_b64)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


class MCAuth:
    """Uses a shared httpx client; close() never closes that shared client.

    save_credentials must atomically persist the supplied dictionary, encrypted.
    It is called for completed login and every token rotation. The caller owns
    pending OAuth state validation, expiry and single-use redemption.
    """

    def __init__(self, http: httpx.AsyncClient, credentials: dict[str, Any],
                 save_credentials: Callable[[dict[str, Any]], Awaitable[None]]):
        self._http = http
        self._credentials = dict(credentials)
        self._save_credentials = save_credentials
        self._token_lock = asyncio.Lock()
        self._http_lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = 1.0

    @staticmethod
    def start_login() -> dict[str, str]:
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        query = urlencode({
            "prompt": "login", "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "nonce": secrets.token_urlsafe(32),
            "CountryCode": "DE", "Language": "de-DE",
        })
        return {"authorization_url": ACCOUNTS + "/connect/authorize?" + query,
                "state": state, "verifier": verifier}

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or ".." in parsed.path.split("/") or not parsed.path
                or any(ord(char) < 32 for char in path)):
            raise ConnectorError("Ungültiger Monsieur-Cuisine-Endpunkt.")
        headers = httpx.Headers({"Accept": "application/json", "Accept-Language": "de-DE",
                   "User-Agent": "Listenbruecke/0.1 (unofficial)",
                   "x-request-id": str(uuid.uuid4())})
        headers.update(kwargs.pop("headers", {}))
        # Never forward credentials across an upstream redirect.
        kwargs.pop("follow_redirects", None)
        kwargs.pop("timeout", None)
        try:
            async with self._http_lock:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                return await self._http.request(method, API_BASE + "/" + path.lstrip("/"),
                                                headers=headers, follow_redirects=False,
                                                timeout=20, **kwargs)
        except httpx.HTTPError:
            raise ConnectorError("Monsieur Cuisine ist derzeit nicht erreichbar.") from None

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        if response.status_code in (401, 403):
            raise AuthenticationRequired("Bitte erneut bei Monsieur Cuisine anmelden.")
        if response.status_code == 429:
            raise ConnectorError("Monsieur Cuisine erhält zu viele Anfragen. Bitte später erneut versuchen.")
        if not response.is_success:
            raise ConnectorError("Monsieur Cuisine hat die Anfrage abgelehnt.")
        try:
            body = response.json()
        except ValueError:
            raise ConnectorError("Monsieur Cuisine hat eine ungültige Antwort geliefert.") from None
        if not isinstance(body, dict) or body.get("code") not in (None, 0):
            raise ConnectorError("Monsieur Cuisine hat die Anfrage abgelehnt.")
        return body

    async def _store(self, credentials: dict[str, Any]) -> None:
        try:
            await self._save_credentials(dict(credentials))
        except Exception:
            raise ConnectorError("Die Monsieur-Cuisine-Anmeldung konnte nicht gespeichert werden.") from None
        self._credentials = dict(credentials)

    async def _app_login(self, tokens: dict[str, Any], companion_id: str) -> dict[str, Any]:
        if not all(isinstance(tokens.get(key), str) and tokens[key]
                   for key in ("access_token", "id_token")):
            raise AuthenticationRequired("Bitte erneut bei Monsieur Cuisine anmelden.")
        key_body = self._body(await self._send("GET", "/api/v1/authkey/server/request-public-key"))
        try:
            server_key = key_body["data"]["serverPublicKey"]
            plaintext = json.dumps({"accessToken": tokens["access_token"],
                                    "idToken": tokens["id_token"], "clientId": CLIENT_ID,
                                    "companionAppId": companion_id}).encode()
            payload, iv, wrapped_key, key = _encrypt_login_payload(plaintext, server_key)
        except Exception:
            raise ConnectorError("Die Monsieur-Cuisine-Anmeldung konnte nicht vorbereitet werden.") from None
        body = self._body(await self._send("POST", "/api/v2/users/login",
            json={"payload": payload, "iv": iv}, headers={
                "Content-Type": "application/json", "x-app-aes": wrapped_key,
                "x-key-id": KEY_ID, "x-encrypt-method": "AES",
                "device-type": "iOS", "appminorversion": "14204",
            }))
        try:
            encrypted = body.get("data", body)
            clear = json.loads(_decrypt_login_response(encrypted["payload"], encrypted["iv"], key))
            if not isinstance(clear, dict) or clear.get("code") not in (None, 0):
                raise ValueError()
            data = clear.get("data", clear)
            jwt = data["accessToken"]
            user = data.get("user", {})
            if not isinstance(jwt, str) or not jwt or not isinstance(user, dict):
                raise ValueError()
        except Exception:
            raise AuthenticationRequired("Die Monsieur-Cuisine-Anmeldung ist fehlgeschlagen. Bitte erneut anmelden.") from None
        return {"jwt": jwt, "lidl_refresh_token": tokens.get("refresh_token"),
                "user_id": user.get("id"), "companion_app_id": companion_id}

    async def complete_login(self, code: str, verifier: str) -> dict[str, Any]:
        if (not isinstance(code, str) or not code or len(code) > 8192
                or not isinstance(verifier, str)
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)):
            raise AuthenticationRequired("Die Anmeldedaten sind ungültig. Bitte die Anmeldung neu starten.")
        async with self._token_lock:
            tokens = self._body(await self._send("POST", "/sso/token", data={
                "grant_type": "authorization_code", "code": code,
                "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
            }))
            companion_id = self._credentials.get("companion_app_id") or str(uuid.uuid4())
            credentials = await self._app_login(tokens, companion_id)
            await self._store(credentials)
            return dict(credentials)

    async def _refresh(self, failed_jwt: str) -> None:
        async with self._token_lock:
            if self._credentials.get("jwt") != failed_jwt:
                return  # Another request has already refreshed the same session.
            response = await self._send("POST", "/api/v1/token/refresh",
                json={"jwt": failed_jwt}, headers={"Authorization": "Bearer " + failed_jwt})
            if response.status_code not in (401, 403):
                body = self._body(response)
                data = body.get("data", body)
                jwt = body.get("jwt") or (data.get("jwt") if isinstance(data, dict) else None)
                if not isinstance(jwt, str) or not jwt:
                    raise AuthenticationRequired("Bitte erneut bei Monsieur Cuisine anmelden.")
                await self._store({**self._credentials, "jwt": jwt})
                return
            refresh_token = self._credentials.get("lidl_refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise AuthenticationRequired("Bitte erneut bei Monsieur Cuisine anmelden.")
            tokens = self._body(await self._send("POST", "/sso/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            }))
            rotated = tokens.get("refresh_token", refresh_token)
            if not isinstance(rotated, str) or not rotated:
                raise AuthenticationRequired("Bitte erneut bei Monsieur Cuisine anmelden.")
            # Preserve rotated refresh credentials even if the following login fails.
            await self._store({**self._credentials, "lidl_refresh_token": rotated})
            companion_id = self._credentials.get("companion_app_id") or str(uuid.uuid4())
            credentials = await self._app_login({**tokens, "refresh_token": rotated}, companion_id)
            await self._store(credentials)

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        jwt = self._credentials.get("jwt")
        if not isinstance(jwt, str) or not jwt:
            raise AuthenticationRequired("Bitte bei Monsieur Cuisine anmelden.")
        headers = httpx.Headers(kwargs.pop("headers", {}))
        headers["Authorization"] = "Bearer " + jwt
        response = await self._send(method, path, headers=headers, **kwargs)
        if response.status_code == 401:
            await self._refresh(jwt)
            headers["Authorization"] = "Bearer " + self._credentials["jwt"]
            response = await self._send(method, path, headers=headers, **kwargs)
        return self._body(response)

    async def close(self) -> None:
        """The application owns and closes the shared http client."""
