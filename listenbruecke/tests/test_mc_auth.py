"""Offline protocol tests; all provider traffic uses httpx.MockTransport."""

import asyncio
import base64
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from listenbruecke.connectors.mc_auth import (
    API_BASE, CLIENT_ID, MCAuth, REDIRECT_URI, _decrypt_login_response,
    _encrypt_login_payload,
)
from listenbruecke.models import AuthenticationRequired, ConnectorError


def encrypted_response(key, body):
    iv = bytes(range(16))
    padder = padding.PKCS7(128).padder()
    clear = json.dumps(body).encode()
    padded = padder.update(clear) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    return {"payload": base64.b64encode(cipher).decode(), "iv": base64.b64encode(iv).decode()}


class CryptoTests(unittest.TestCase):
    def test_login_crypto_interoperates_with_server_rsa_and_aes(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        payload, iv, wrapped, key = _encrypt_login_payload(b'{"hello":"world"}', public)
        self.assertEqual(private.decrypt(base64.b64decode(wrapped), rsa_padding.PKCS1v15()), key)
        self.assertEqual(_decrypt_login_response(payload, iv, key), b'{"hello":"world"}')
        response = encrypted_response(key, {"data": {"accessToken": "jwt-secret"}})
        self.assertEqual(json.loads(_decrypt_login_response(response["payload"], response["iv"], key))["data"]["accessToken"], "jwt-secret")
        self.assertNotEqual(_encrypt_login_payload(b"x", public)[3], key)

    def test_pkce_has_opaque_state_and_no_callback_or_ingress_url(self):
        login = MCAuth.start_login()
        fields = parse_qs(urlsplit(login["authorization_url"]).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(login["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(fields["code_challenge"], [expected])
        self.assertEqual(fields["code_challenge_method"], ["S256"])
        self.assertEqual(fields["state"], [login["state"]])
        self.assertEqual(fields["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(fields["client_id"], [CLIENT_ID])
        self.assertRegex(login["state"], r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn("callback", login["state"])
        self.assertNotIn("ingress", login["authorization_url"])
        self.assertNotEqual(login["state"], MCAuth.start_login()["state"])


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = []
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def auth(self, handler, credentials=None, save=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        async def store(value):
            self.saved.append(dict(value))
        auth = MCAuth(client, credentials if credentials is not None else {"jwt": "old-jwt-secret"}, save or store)
        auth._min_interval = 0
        return auth

    def login_handler(self, requests, *, fail_login=False):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        def handler(request):
            requests.append(request)
            if request.url.path == "/sso/token":
                return httpx.Response(200, json={"access_token": "lidl-access-secret", "id_token": "lidl-id-secret", "refresh_token": "rotated-refresh-secret"})
            if request.url.path.endswith("request-public-key"):
                return httpx.Response(200, json={"code": 0, "data": {"serverPublicKey": public}})
            if request.url.path == "/api/v2/users/login":
                key = private.decrypt(base64.b64decode(request.headers["x-app-aes"]), rsa_padding.PKCS1v15())
                encrypted = json.loads(request.content)
                clear = json.loads(_decrypt_login_response(encrypted["payload"], encrypted["iv"], key))
                self.assertEqual(clear["accessToken"], "lidl-access-secret")
                self.assertEqual(clear["idToken"], "lidl-id-secret")
                self.assertEqual(clear["clientId"], CLIENT_ID)
                body = ({"code": 40001, "message": "remote-secret"} if fail_login else
                        {"code": 0, "data": {"accessToken": "new-jwt-secret", "user": {"id": 17}}})
                return httpx.Response(200, json={"code": 0, "data": encrypted_response(key, body)})
            raise AssertionError("Unexpected endpoint")
        return handler

    async def test_complete_login_exchanges_and_decrypts_then_persists(self):
        requests = []
        auth = self.auth(self.login_handler(requests), {})
        login = auth.start_login()
        credentials = await auth.complete_login("one-time & code", login["verifier"])
        form = parse_qs(requests[0].content.decode())
        self.assertEqual(form["code"], ["one-time & code"])
        self.assertEqual(form["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(form["code_verifier"], [login["verifier"]])
        self.assertEqual(credentials["jwt"], "new-jwt-secret")
        self.assertEqual(credentials["user_id"], 17)
        self.assertEqual(credentials["lidl_refresh_token"], "rotated-refresh-secret")
        self.assertEqual(self.saved, [credentials])

    async def test_401_refreshes_once_and_returns_raw_provider_shape(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith("token/refresh"):
                return httpx.Response(200, json={"data": {"jwt": "new-jwt-secret"}})
            if request.headers["authorization"] == "Bearer old-jwt-secret":
                return httpx.Response(401, json={"message": "remote-secret"})
            return httpx.Response(200, json={"code": 0, "data": {"shoppingListCustomItems": []}})
        auth = self.auth(handler)
        result = await auth.request("GET", "api/v1/users/shoppingList", headers={"authorization": "untrusted-secret"})
        self.assertEqual(result, {"code": 0, "data": {"shoppingListCustomItems": []}})
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].headers.get_list("authorization"), ["Bearer old-jwt-secret"])
        self.assertEqual(self.saved[-1]["jwt"], "new-jwt-secret")

    async def test_second_401_requires_login_without_refresh_loop(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"jwt": "new-jwt-secret"}) if request.url.path.endswith("token/refresh") else httpx.Response(401, json={"secret": "remote-secret"})
        with self.assertRaises(AuthenticationRequired) as caught:
            await self.auth(handler).request("GET", "/api/v1/users/shoppingList")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(requests), 3)

    async def test_rotated_lidl_refresh_is_saved_before_later_failure(self):
        def handler(request):
            if request.url.path == "/sso/token":
                return httpx.Response(200, json={"access_token": "new-secret", "id_token": "id-secret", "refresh_token": "rotated-secret"})
            if request.url.path.endswith("request-public-key"):
                return httpx.Response(503, text="remote-secret")
            return httpx.Response(401, json={})
        auth = self.auth(handler, {"jwt": "old-jwt-secret", "lidl_refresh_token": "old-refresh-secret"})
        with self.assertRaises(ConnectorError):
            await auth.request("GET", "/api/v1/users/shoppingList")
        self.assertEqual(self.saved[-1]["lidl_refresh_token"], "rotated-secret")

    async def test_lidl_refresh_fallback_recovers_failed_jwt_refresh(self):
        requests = []
        login_handler = self.login_handler(requests)
        def handler(request):
            if request.url.path == "/api/v1/users/shoppingList":
                return (httpx.Response(200, json={"data": {}})
                        if request.headers["authorization"] == "Bearer new-jwt-secret"
                        else httpx.Response(401, json={}))
            if request.url.path.endswith("token/refresh"):
                return httpx.Response(401, json={})
            return login_handler(request)
        auth = self.auth(handler, {"jwt": "old-jwt-secret", "lidl_refresh_token": "old-refresh-secret", "companion_app_id": "stable-id"})
        self.assertEqual(await auth.request("GET", "api/v1/users/shoppingList"), {"data": {}})
        self.assertEqual(self.saved[-1]["companion_app_id"], "stable-id")
        self.assertEqual(self.saved[-1]["lidl_refresh_token"], "rotated-refresh-secret")
        self.assertEqual(parse_qs(requests[0].content.decode())["grant_type"], ["refresh_token"])

    async def test_concurrent_401s_share_one_refresh(self):
        refreshes = 0
        async def handler(request):
            nonlocal refreshes
            await asyncio.sleep(0)
            if request.url.path.endswith("token/refresh"):
                refreshes += 1
                return httpx.Response(200, json={"jwt": "new-jwt-secret"})
            return (httpx.Response(401, json={})
                    if request.headers["authorization"] == "Bearer old-jwt-secret"
                    else httpx.Response(200, json={"ok": True}))
        auth = self.auth(handler)
        results = await asyncio.gather(*(auth.request("GET", "api/v1/users/shoppingList") for _ in range(3)))
        self.assertEqual(results, [{"ok": True}] * 3)
        self.assertEqual(refreshes, 1)

    async def test_invalid_verifier_never_contacts_provider(self):
        requests = []
        auth = self.auth(lambda request: requests.append(request) or httpx.Response(200, json={}), {})
        for code, verifier in (("", "a" * 43), ("secret", "short"), ("secret", "!" * 43)):
            with self.assertRaises(AuthenticationRequired):
                await auth.complete_login(code, verifier)
        self.assertEqual(requests, [])

    async def test_encrypted_login_business_error_is_sanitized(self):
        auth = self.auth(self.login_handler([], fail_login=True), {})
        with self.assertRaises(AuthenticationRequired) as caught:
            await auth.complete_login("code-secret", auth.start_login()["verifier"])
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.saved, [])

    async def test_provider_http_json_and_transport_errors_are_sanitized(self):
        for response in (httpx.Response(403, text="secret"), httpx.Response(500, text="secret"),
                         httpx.Response(200, text="secret"), httpx.Response(200, json={"code": 123, "message": "secret"}),
                         httpx.Response(302, headers={"location": "https://evil.invalid/secret"})):
            auth = self.auth(lambda request, response=response: response)
            with self.assertRaises(ConnectorError) as caught:
                await auth.request("GET", "/api/v1/users/shoppingList")
            self.assertNotIn("secret", str(caught.exception))
        def failing(request):
            raise httpx.ConnectError("transport-secret")
        with self.assertRaises(ConnectorError) as caught:
            await self.auth(failing).request("GET", "/api/v1/users/shoppingList")
        self.assertNotIn("secret", str(caught.exception))

    async def test_external_paths_rejected_and_shared_http_not_closed(self):
        calls = []
        auth = self.auth(lambda request: calls.append(request) or httpx.Response(200, json={}))
        for path in ("https://evil.invalid/", "//evil.invalid/api", "/../auth", "/api/x?token=secret"):
            with self.assertRaises(ConnectorError):
                await auth.request("GET", path)
        self.assertFalse(calls)
        await auth.close()
        self.assertFalse(auth._http.is_closed)

    async def test_missing_login_and_failed_storage_never_expose_secrets(self):
        auth = self.auth(lambda request: httpx.Response(200, json={}), {})
        with self.assertRaises(AuthenticationRequired):
            await auth.request("GET", "/api/v1/users/shoppingList")
        async def failing_store(credentials):
            raise RuntimeError("save-secret")
        auth = self.auth(self.login_handler([]), {}, failing_store)
        with self.assertRaises(ConnectorError) as caught:
            await auth.complete_login("code-secret", auth.start_login()["verifier"])
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
