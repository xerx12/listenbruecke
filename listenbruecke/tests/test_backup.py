"""Synthetic-only opaque backups, interrupted restore and authenticated import."""

import base64
import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from listenbruecke import backup
from listenbruecke.main import create_app
from listenbruecke.store import Store


@pytest.fixture
def capsule(tmp_path):
    directory = tmp_path / "source"
    store = Store(directory)
    store.set_password("synthetic-existing-password")
    store.set("sync_settings", {"enabled": True, "targets": [{"provider": "bring", "list_id": "list"}], "interval_seconds": 60})
    store.set("account_generation", 7)
    store.set("sync:example", {"records": {"stable-id": {"refs": {"bring:list": "native-uid"}}}})
    store.set("padding", "x" * 50000)
    store.set_secret("bring", {"email": "synthetic@example.invalid", "password": "fixture-password"})
    store.set_secret("monsieur_cuisine", {"jwt": "fixture-jwt"})
    store.event("info", "Synthetischer Eintrag")
    session = store.new_session()
    store.close()
    with patch.object(Fernet, "decrypt", side_effect=AssertionError("Backup must remain opaque")):
        archive = backup.create_backup(directory)
    return directory, archive, session


def repack(archive, change=None, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(io.BytesIO(archive)) as original:
        files = {entry.filename: original.read(entry) for entry in original.infolist()}
    if change:
        change(files)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression) as output:
        for name, raw in files.items():
            output.writestr(name, raw)
    return result.getvalue()


def test_cold_restore_preserves_all_state_key_and_existing_login(capsule, tmp_path):
    source, archive, session = capsule
    target = tmp_path / "target"
    current = Store(target)
    current.set("old-target", "rollback-state")
    summary = backup.stage_import(target, archive)
    assert current.get("old-target") == "rollback-state"  # No open DB replacement.
    assert current.get_secret("bring") is None
    current.close()
    with patch.object(Fernet, "decrypt", side_effect=AssertionError("Restore must remain opaque")):
        assert backup.apply_pending_import(target)
        assert not backup.apply_pending_import(target)
        actual = backup.backup_summary(target)
    assert actual["last_import"]["key_sha256"] == summary["key_sha256"]
    assert actual["last_import"]["database_sha256"] == summary["database_sha256"]
    assert actual["row_counts"] == summary["row_counts"]
    restored = Store(target)
    try:
        assert restored.check_password("synthetic-existing-password")
        assert restored.valid_session(session)
        assert restored.get("sync_settings")["enabled"]
        assert restored.get("account_generation") == 7
        assert restored.get("sync:example")["records"]["stable-id"]["refs"]["bring:list"] == "native-uid"
        assert restored.get_secret("bring")["email"] == "synthetic@example.invalid"
    finally:
        restored.close()
    assert len(list((target / ".before-import").glob("*.zip"))) == 1
    assert stat.S_IMODE((target / "encryption.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((target / "app.sqlite3").stat().st_mode) == 0o600


def test_partial_restore_retries_same_intact_stage_and_keeps_original_backup(capsule, tmp_path):
    _, archive, _ = capsule
    target = tmp_path / "target"
    original = Store(target)
    original.set("old-target", "rollback-state")
    original.close()
    backup.stage_import(target, archive)
    write = backup._atomic_write
    def interrupted(path, raw):
        if path.parent == target and path.name == "app.sqlite3":
            raise OSError("simulated interruption")
        return write(path, raw)
    with patch.object(backup, "_atomic_write", interrupted), pytest.raises(backup.BackupError):
        backup.apply_pending_import(target)
    assert (target / ".pending-import" / "encryption.key").is_file()
    before = list((target / ".before-import").glob("*.zip"))
    assert len(before) == 1
    fingerprint = hashlib.sha256(before[0].read_bytes()).digest()
    assert backup.apply_pending_import(target)
    assert hashlib.sha256(before[0].read_bytes()).digest() == fingerprint
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    backup.stage_import(rollback, before[0].read_bytes())
    backup.apply_pending_import(rollback)
    store = Store(rollback)
    try:
        assert store.get("old-target") == "rollback-state"
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["traversal", "extra", "missing", "hash", "schema", "key", "sqlite", "symlink", "encrypted"])
def test_malformed_archives_never_replace_live_files(capsule, tmp_path, kind):
    _, archive, _ = capsule
    target = tmp_path / "target"
    target.mkdir()
    def change(files):
        if kind in ("traversal", "extra"):
            files["../outside" if kind == "traversal" else "unexpected"] = b"no"
        elif kind == "missing":
            files.pop("encryption.key")
        elif kind == "hash":
            files["app.sqlite3"] += b"bad"
        else:
            manifest = json.loads(files["manifest.json"])
            if kind == "schema":
                manifest["schema"] = 999
            elif kind in ("key", "sqlite"):
                name = "encryption.key" if kind == "key" else "app.sqlite3"
                files[name] = b"invalid-data"
                manifest["files"][name] = {"sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])}
            files["manifest.json"] = json.dumps(manifest).encode()
    broken = repack(archive, change)
    if kind == "symlink":
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as dest:
                for name in source.namelist():
                    info = zipfile.ZipInfo(name)
                    if name == "encryption.key":
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    dest.writestr(info, source.read(name))
            broken = output.getvalue()
    elif kind == "encrypted":
        raw = bytearray(broken)
        location = raw.index(b"PK\x01\x02")
        raw[location + 8] |= 1
        broken = bytes(raw)
    with pytest.raises(backup.BackupError):
        backup.stage_import(target, broken)
    assert not (target / ".pending-import").exists()
    assert not (tmp_path / "outside").exists()


def test_duplicate_entries_and_archive_limit_are_rejected(capsule, tmp_path):
    _, archive, _ = capsule
    output = io.BytesIO(archive)
    with pytest.warns(UserWarning), zipfile.ZipFile(output, "a") as duplicate:
        duplicate.writestr("manifest.json", b"{}")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(backup.BackupError):
        backup.stage_import(target, output.getvalue())
    with patch.object(backup, "MAX_ARCHIVE_BYTES", 100), pytest.raises(backup.BackupError):
        backup.stage_import(target, archive)


def test_summary_contains_only_counts_and_fingerprints_without_decryption(capsule):
    source, _, _ = capsule
    with patch.object(Fernet, "decrypt", side_effect=AssertionError("No decryption")):
        summary = backup.backup_summary(source)
    assert set(summary) == {"schema", "row_counts", "database_sha256", "key_sha256", "bytes", "pending_import", "last_import"}
    assert set(summary["row_counts"]) == {"settings", "secrets", "events", "sessions"}
    assert "fixture-" not in json.dumps(summary)
    assert "synthetic@example" not in json.dumps(summary)


def test_authenticated_import_large_route_stages_then_applies_before_store_open(capsule, tmp_path, monkeypatch):
    _, archive, session = capsule
    archive = repack(archive, compression=zipfile.ZIP_STORED)
    encoded = base64.b64encode(archive).decode()
    assert len(encoded) > 32768
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("LISTENBRUECKE_ALLOWED_HOSTS", "testserver")
    target = tmp_path / "target"
    app = create_app(target, background=False)
    with TestClient(app) as client:
        assert client.post("/api/backup/import", json={"archive": encoded}).status_code == 401
        assert client.get("/api/backup/summary").status_code == 401
        assert client.post("/api/setup", json={"password": "target-local-password"}).status_code == 200
        assert client.post("/api/settings", json={"archive": encoded}).status_code == 413
        response = client.post("/api/backup/import", json={"archive": encoded})
        assert response.status_code == 200, response.text
        assert response.json()["requires_restart"]
        assert "archive" not in response.json()
        assert app.state.service.store.get_secret("bring") is None
        assert client.put("/api/settings", json={"enabled": False}).status_code == 409
        assert client.get("/api/backup/summary").json()["pending_import"]
    with TestClient(create_app(target, background=False)) as client:
        assert client.post("/api/login", json={"password": "synthetic-existing-password"}).status_code == 200
        response = client.get("/api/backup/summary")
        assert response.status_code == 200
        assert not response.json()["pending_import"]
        assert response.json()["last_import"]["row_counts"]["secrets"] == 2
