"""Cold, opaque state transfer. Never decrypt credentials or log archive data.

The operator must quiesce the source and stop all writers before applying a
pending import. A backup contains the encryption key: keep it private.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from listenbruecke.models import ConnectorError

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
IMPORT_JSON_BYTES = 32 * 1024 * 1024
NAMES = {"app.sqlite3", "encryption.key", "manifest.json"}
TABLES = {"settings": {"key", "value"}, "secrets": {"key", "value"},
          "events": {"id", "time", "level", "message"}, "sessions": {"digest", "expires"}}


class BackupError(ConnectorError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _receipt(value):
    fields = ("schema", "row_counts", "database_sha256", "key_sha256", "bytes")
    result = {name: value[name] for name in fields}
    if (type(result["schema"]) is not int or result["schema"] != 1
            or type(result["bytes"]) is not int or not 0 < result["bytes"] <= MAX_ARCHIVE_BYTES
            or set(result["row_counts"]) != set(TABLES)
            or any(type(count) is not int or count < 0 for count in result["row_counts"].values())
            or any(not isinstance(result[name], str) or not re.fullmatch(r"[a-f0-9]{64}", result[name])
                   for name in ("database_sha256", "key_sha256"))):
        raise ValueError()
    return result


def _read_regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupError("Die Sicherungsdateien sind unvollständig oder ungültig.")
    return path.read_bytes()


def _connect_readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _inspect_database(raw):
    with tempfile.TemporaryDirectory(prefix="listenbruecke-check-") as temporary:
        path = Path(temporary) / "app.sqlite3"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        with closing(_connect_readonly(path)) as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise BackupError("Die Sicherungsdatenbank ist beschädigt.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES) <= tables:
                raise BackupError("Die Sicherung hat ein unbekanntes Datenformat.")
            for table, columns in TABLES.items():
                found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns <= found:
                    raise BackupError("Die Sicherung hat ein unbekanntes Datenformat.")
            return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in TABLES}


def _snapshot(directory):
    database = directory / "app.sqlite3"
    _read_regular(database)
    with tempfile.TemporaryDirectory(prefix="listenbruecke-backup-") as temporary:
        target = Path(temporary) / "app.sqlite3"
        source = _connect_readonly(database)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        raw = target.read_bytes()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise BackupError("Die Sicherung ist zu groß.")
    return raw


def _summary(database, key, *, archive_bytes=None):
    Fernet(key)  # Validate format only. Never call decrypt on any stored value.
    result = {"schema": 1, "row_counts": _inspect_database(database),
              "database_sha256": _sha(database), "key_sha256": _sha(key),
              "bytes": len(database) + len(key)}
    if archive_bytes is not None:
        result["bytes"] = archive_bytes
    return result


def create_backup(data_dir):
    """Return a private ZIP. Caller must have quiesced the source service."""
    try:
        directory = Path(data_dir)
        database, key = _snapshot(directory), _read_regular(directory / "encryption.key")
        summary = _summary(database, key)
        manifest = {"schema": 1, "row_counts": summary["row_counts"], "files": {
            "app.sqlite3": {"sha256": _sha(database), "bytes": len(database)},
            "encryption.key": {"sha256": _sha(key), "bytes": len(key)},
        }}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in (("app.sqlite3", database), ("encryption.key", key),
                                  ("manifest.json", json.dumps(manifest, sort_keys=True).encode())):
                archive.writestr(name, content)
        raw = output.getvalue()
        if len(raw) > MAX_ARCHIVE_BYTES or sum(len(value) for value in (database, key, json.dumps(manifest).encode())) > MAX_ARCHIVE_BYTES:
            raise BackupError("Die Sicherung ist zu groß.")
        return raw
    except BackupError:
        raise
    except Exception:
        raise BackupError("Die Sicherung konnte nicht erstellt werden.") from None


def _validate_archive(raw):
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
            raise ValueError()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 3 or {entry.filename for entry in entries} != NAMES:
                raise ValueError()
            if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError()
            for entry in entries:
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (entry.flag_bits & 1 or entry.is_dir() or stat.S_ISLNK(mode)
                        or kind not in (0, stat.S_IFREG)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError()
            files = {entry.filename: archive.read(entry) for entry in entries}
        if len(files["manifest.json"]) > 65536 or len(files["encryption.key"]) > 1024:
            raise ValueError()
        manifest = json.loads(files["manifest.json"])
        if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
                or manifest["schema"] != 1 or set(manifest.get("files", {})) != NAMES - {"manifest.json"}):
            raise ValueError()
        for name in ("app.sqlite3", "encryption.key"):
            if manifest["files"][name] != {"sha256": _sha(files[name]), "bytes": len(files[name])}:
                raise ValueError()
        summary = _summary(files["app.sqlite3"], files["encryption.key"], archive_bytes=len(raw))
        if summary["row_counts"] != manifest.get("row_counts"):
            raise ValueError()
        if any(type(value) is not int or value < 0 for value in manifest["row_counts"].values()):
            raise ValueError()
        return files, summary
    except Exception:
        raise BackupError("Die Sicherung ist ungültig, beschädigt oder zu groß.") from None


def _sync_directory(path):
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(path, raw):
    fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stage_import(data_dir, archive):
    """Validate and stage privately; never replace an open database."""
    files, summary = _validate_archive(archive)
    directory = Path(data_dir)
    pending = directory / ".pending-import"
    if pending.exists() or pending.is_symlink():
        raise BackupError("Eine Wiederherstellung wartet bereits auf den Neustart.")
    temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=directory))
    try:
        os.chmod(temporary, 0o700)
        for name, raw in files.items():
            _atomic_write(temporary / name, raw)
        _atomic_write(temporary / "restore.json", json.dumps({"stage_id": uuid.uuid4().hex,
                      "summary": summary}, sort_keys=True).encode())
        os.rename(temporary, pending)
        _sync_directory(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return summary


def apply_pending_import(data_dir):
    """Apply before Store opens. Interruption leaves the complete staging intact."""
    directory = Path(data_dir)
    pending = directory / ".pending-import"
    if not pending.exists() and not pending.is_symlink():
        return False
    try:
        if pending.is_symlink() or not pending.is_dir():
            raise ValueError()
        metadata = json.loads(_read_regular(pending / "restore.json"))
        stored_summary = _receipt(metadata["summary"])
        stage_id = metadata["stage_id"]
        if not isinstance(stage_id, str) or not re.fullmatch(r"[a-f0-9]{32}", stage_id):
            raise ValueError()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in NAMES:
                archive.writestr(name, _read_regular(pending / name))
        files, summary = _validate_archive(output.getvalue())
        if any(summary[name] != stored_summary[name]
               for name in ("schema", "row_counts", "database_sha256", "key_sha256")):
            raise ValueError()
        before = directory / ".before-import"
        if before.is_symlink():
            raise ValueError()
        before.mkdir(mode=0o700, exist_ok=True)
        os.chmod(before, 0o700)
        _sync_directory(directory)
        previous = before / (stage_id + ".zip")
        if previous.is_symlink():
            raise ValueError()
        if not previous.exists() and (directory / "app.sqlite3").exists():
            _atomic_write(previous, create_backup(directory))
        for name in ("app.sqlite3-wal", "app.sqlite3-shm"):
            path = directory / name
            if path.exists() or path.is_symlink():
                path.unlink()
        for name in ("encryption.key", "app.sqlite3"):
            _atomic_write(directory / name, files[name])
        if any(_sha(_read_regular(directory / name)) != _sha(files[name])
               for name in ("encryption.key", "app.sqlite3")):
            raise ValueError()
        receipt = {**summary, "bytes": stored_summary["bytes"]}
        _atomic_write(directory / ".last-import.json", json.dumps(receipt, sort_keys=True).encode())
        completed = directory / (".completed-import-" + stage_id)
        os.rename(pending, completed)
        _sync_directory(directory)
        shutil.rmtree(completed, ignore_errors=True)
        return True
    except Exception:
        raise BackupError("Die Wiederherstellung konnte nicht abgeschlossen werden. Die vorbereitete Sicherung bleibt für einen erneuten Start erhalten.") from None


def backup_summary(data_dir):
    """Return counts and opaque fingerprints only, never private row values."""
    try:
        directory = Path(data_dir)
        result = _summary(_snapshot(directory), _read_regular(directory / "encryption.key"))
        result["pending_import"] = (directory / ".pending-import").exists()
        receipt = directory / ".last-import.json"
        result["last_import"] = _receipt(json.loads(_read_regular(receipt))) if receipt.exists() else None
        return result
    except BackupError:
        raise
    except Exception:
        raise BackupError("Die Sicherungsübersicht konnte nicht erstellt werden.") from None
