"""Initialize the app's private data volume, then drop container root privileges."""

import os
from pathlib import Path


def main():
    directory = Path(os.environ.get("LISTENBRUECKE_DATA", "/data"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        # CAP_DAC_OVERRIDE is intentionally absent. Root must retain ownership
        # while accessing children of a private 0700 volume, including a volume
        # already owned by the application from an earlier container start.
        os.chown(directory, 0, 0, follow_symlinks=False)
        # Only our own fixed filenames, never another add-on's configuration.
        for name in ("encryption.key", "app.sqlite3", "app.sqlite3-wal", "app.sqlite3-shm", ".last-import.json"):
            path = directory / name
            if not path.is_symlink() and path.is_file():
                os.chown(path, 1000, 1000, follow_symlinks=False)
        pending = directory / ".pending-import"
        if not pending.is_symlink() and pending.is_dir():
            os.chown(pending, 0, 0, follow_symlinks=False)
            for name in ("encryption.key", "app.sqlite3", "manifest.json", "restore.json"):
                path = pending / name
                if not path.is_symlink() and path.is_file():
                    os.chown(path, 1000, 1000, follow_symlinks=False)
            os.chown(pending, 1000, 1000, follow_symlinks=False)
        # This must be the final filesystem operation before switching IDs:
        # after handoff, root cannot traverse a 0700 directory without DAC_OVERRIDE.
        os.chown(directory, 1000, 1000, follow_symlinks=False)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    from listenbruecke.backup import apply_pending_import
    apply_pending_import(directory)
    from listenbruecke.main import run
    run()


if __name__ == "__main__":
    main()
