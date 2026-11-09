"""Initialize the app's private data volume, then drop container root privileges."""

import os
from pathlib import Path


def main():
    directory = Path(os.environ.get("LISTENBRUECKE_DATA", "/data"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(directory, 1000, 1000)
        # Only our own fixed filenames, never another add-on's configuration.
        for name in ("encryption.key", "app.sqlite3", "app.sqlite3-wal", "app.sqlite3-shm", ".last-import.json"):
            path = directory / name
            if path.exists() and not path.is_symlink():
                os.chown(path, 1000, 1000)
        pending = directory / ".pending-import"
        if pending.is_dir() and not pending.is_symlink():
            os.chown(pending, 1000, 1000)
            for name in ("encryption.key", "app.sqlite3", "manifest.json", "restore.json"):
                path = pending / name
                if path.is_file() and not path.is_symlink():
                    os.chown(path, 1000, 1000)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    from listenbruecke.backup import apply_pending_import
    apply_pending_import(directory)
    from listenbruecke.main import run
    run()


if __name__ == "__main__":
    main()
