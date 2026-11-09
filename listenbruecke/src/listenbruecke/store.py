"""Private SQLite state with authenticated encryption for account credentials."""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        key_path = directory / "encryption.key"
        database = directory / "app.sqlite3"
        if not key_path.exists():
            if database.exists():
                raise RuntimeError("Der Schlüssel zum Datenverzeichnis fehlt. Bitte das vollständige Backup wiederherstellen.")
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self._cipher = Fernet(key_path.read_bytes())
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        os.chmod(database, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, value BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
        """)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def get(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        raw = json.dumps(value, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, raw))

    def get_secret(self, key):
        with self._lock:
            row = self._db.execute("SELECT value FROM secrets WHERE key=?", (key,)).fetchone()
        return json.loads(self._cipher.decrypt(row[0])) if row else None

    def set_secret(self, key, value):
        raw = self._cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())
        with self._lock, self._db:
            self._db.execute("INSERT INTO secrets VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, raw))

    def delete_secret(self, key):
        with self._lock, self._db:
            self._db.execute("DELETE FROM secrets WHERE key=?", (key,))

    def consume_secret(self, key):
        with self._lock, self._db:
            # A single SQLite statement also protects against another Store
            # connection or worker consuming the same pending login concurrently.
            rows = self._db.execute("DELETE FROM secrets WHERE key=? RETURNING value", (key,)).fetchall()
        return json.loads(self._cipher.decrypt(rows[0][0])) if rows else None

    def event(self, level, message):
        with self._lock, self._db:
            self._db.execute("INSERT INTO events(time,level,message) VALUES (?,?,?)", (now(), level, message))
            self._db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)")

    def events(self, limit=50):
        with self._lock:
            rows = self._db.execute("SELECT time,level,message FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("time", "level", "message"), row)) for row in rows]

    def setup_required(self):
        return self.get("password") is None

    def set_password(self, password):
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        self.set("password", {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()})

    def check_password(self, password):
        stored = self.get("password")
        if not stored:
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(stored["salt"]), n=16384, r=8, p=1)
        return secrets.compare_digest(digest, base64.b64decode(stored["hash"]))

    def new_session(self):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self._db.execute("INSERT INTO sessions VALUES (?,?)", (digest, time.time() + 86400))
        return token

    def valid_session(self, token):
        if not token or len(token) > 256:
            return False
        with self._lock:
            row = self._db.execute("SELECT expires FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return bool(row and row[0] > time.time())

    def delete_session(self, token):
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))
