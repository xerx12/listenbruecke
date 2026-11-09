"""Persistence, encryption and one-time-login state invariants."""

import concurrent.futures
import hashlib
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from listenbruecke.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "data"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.path)

    def test_credentials_encrypted_on_disk_and_key_survives_restart(self):
        key = (self.path / "encryption.key").read_bytes()
        secret = {"password": "PASSWORD-UNIQUE-SECRET", "jwt": "JWT-UNIQUE-SECRET"}
        self.store.set_secret("provider", secret)
        for file in self.path.glob("app.sqlite3*"):
            raw = file.read_bytes()
            self.assertNotIn(b"PASSWORD-UNIQUE-SECRET", raw)
            self.assertNotIn(b"JWT-UNIQUE-SECRET", raw)
        self.restart()
        self.assertEqual((self.path / "encryption.key").read_bytes(), key)
        self.assertEqual(self.store.get_secret("provider"), secret)
        self.assertEqual(stat.S_IMODE((self.path / "encryption.key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.path / "app.sqlite3").stat().st_mode), 0o600)

    def test_missing_key_for_existing_database_never_creates_a_replacement(self):
        self.store.set_secret("provider", {"jwt": "secret"})
        (self.path / "encryption.key").unlink()
        with self.assertRaises(RuntimeError):
            Store(self.path)
        self.assertFalse((self.path / "encryption.key").exists())

    def test_password_is_hashed_and_checked_after_restart(self):
        self.assertTrue(self.store.setup_required())
        self.store.set_password("correct horse battery PASSWORD-SECRET")
        self.assertFalse(self.store.setup_required())
        self.assertTrue(self.store.check_password("correct horse battery PASSWORD-SECRET"))
        self.assertFalse(self.store.check_password("wrong"))
        self.assertNotIn("PASSWORD-SECRET", str(self.store.get("password")))
        self.restart()
        self.assertTrue(self.store.check_password("correct horse battery PASSWORD-SECRET"))

    def test_session_token_is_hashed_expires_and_can_be_revoked(self):
        with patch("listenbruecke.store.time.time", return_value=1000):
            token = self.store.new_session()
            self.assertTrue(self.store.valid_session(token))
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertIsNotNone(self.store._db.execute("SELECT digest FROM sessions WHERE digest=?", (digest,)).fetchone())
        self.assertNotIn(token.encode(), (self.path / "app.sqlite3-wal").read_bytes())
        with patch("listenbruecke.store.time.time", return_value=1000 + 86401):
            self.assertFalse(self.store.valid_session(token))
        with patch("listenbruecke.store.time.time", return_value=1001):
            self.store.delete_session(token)
            self.assertFalse(self.store.valid_session(token))
        self.assertFalse(self.store.valid_session("x" * 257))

    def test_secret_can_be_consumed_only_once_across_store_connections(self):
        other = Store(self.path)
        self.store.set_secret("pending", {"state": "opaque", "verifier": "secret"})
        barrier = threading.Barrier(2)
        def trace(sql):
            if sql.startswith("DELETE FROM secrets"):
                barrier.wait(timeout=5)
        self.store._db.set_trace_callback(trace)
        other._db.set_trace_callback(trace)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(store.consume_secret, "pending") for store in (self.store, other)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertIsNone(self.store.get_secret("pending"))
        finally:
            self.store._db.set_trace_callback(None)
            other._db.set_trace_callback(None)
            other.close()


if __name__ == "__main__":
    unittest.main()
