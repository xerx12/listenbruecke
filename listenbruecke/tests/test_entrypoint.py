"""Verify private-volume bootstrap ordering without changing test-process IDs."""

import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("container_entrypoint", Path(__file__).parents[1] / "entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    volume = tmp_path / "data"
    volume.mkdir(mode=0o700)
    pending = volume / ".pending-import"
    pending.mkdir(mode=0o700)
    for directory, names in ((volume, ("encryption.key", "app.sqlite3")),
                             (pending, ("encryption.key", "app.sqlite3", "manifest.json", "restore.json"))):
        for name in names:
            (directory / name).write_text("synthetic bootstrap fixture")
    monkeypatch.setenv("LISTENBRUECKE_DATA", str(volume))
    events = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda path, uid, gid, **kwargs: events.append(("chown", Path(path), uid, gid, kwargs)))
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(module.os, name, lambda value, name=name: events.append((name, value)))
    backup = ModuleType("listenbruecke.backup")
    backup.apply_pending_import = lambda directory: events.append(("import", directory))
    main = ModuleType("listenbruecke.main")
    main.run = lambda: events.append(("run",))
    monkeypatch.setitem(sys.modules, "listenbruecke.backup", backup)
    monkeypatch.setitem(sys.modules, "listenbruecke.main", main)
    return module, volume, pending, events


def test_volume_and_pending_directories_are_handed_off_after_their_files(bootstrap):
    module, volume, pending, events = bootstrap
    module.main()
    chowns = [event for event in events if event[0] == "chown"]
    assert chowns[0][1:4] == (volume, 0, 0)
    assert chowns[-1][1:4] == (volume, 1000, 1000)
    pending_root = next(i for i, event in enumerate(chowns) if event[1:4] == (pending, 0, 0))
    pending_app = next(i for i, event in enumerate(chowns) if event[1:4] == (pending, 1000, 1000))
    assert pending_root < pending_app
    assert all(pending_root < i < pending_app for i, event in enumerate(chowns) if event[1].parent == pending)
    assert all(event[4] == {"follow_symlinks": False} for event in chowns)
    handoff = events.index(chowns[-1])
    assert events[handoff + 1:handoff + 4] == [("setgroups", []), ("setgid", 1000), ("setuid", 1000)]
    assert events[handoff + 4:] == [("import", volume), ("run",)]


def test_known_file_symlinks_are_skipped(bootstrap, tmp_path):
    module, volume, pending, events = bootstrap
    external = tmp_path / "unrelated.txt"
    external.write_text("synthetic unrelated file")
    (volume / "encryption.key").unlink()
    (volume / "encryption.key").symlink_to(external)
    (pending / "app.sqlite3").unlink()
    (pending / "app.sqlite3").symlink_to(external)
    module.main()
    touched = [event[1] for event in events if event[0] == "chown"]
    assert volume / "encryption.key" not in touched
    assert pending / "app.sqlite3" not in touched
    assert external not in touched
