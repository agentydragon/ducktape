"""Create transactionally consistent copies of Codex's session index databases."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    codex_home = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    # Codex stores these indexes in state_N.sqlite files below CODEX_HOME. SQLite's
    # backup API takes a consistent snapshot while Codex continues writing to the DB.
    databases = sorted(codex_home.rglob("state_*.sqlite"))
    for database in databases:
        relative_path = database.relative_to(codex_home)
        if {"cache", "plugins"}.intersection(relative_path.parts):
            continue
        snapshot_dir = output_dir / relative_path.parent
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        temporary = snapshot_dir / f".{database.name}.tmp"
        source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        backup = sqlite3.connect(temporary)
        try:
            source.backup(backup)
            backup.commit()
        finally:
            backup.close()
            source.close()

        with temporary.open("rb") as snapshot:
            digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
        destination = snapshot_dir / f"{database.name}-{digest}.backup"
        temporary.replace(destination)
        # Stable paths and timestamps make an unchanged database snapshot a no-op
        # for Restic, while a changed database gets a new content-addressed filename.
        os.utime(destination, ns=(0, 0))

    for directory in (path for path in output_dir.rglob("*") if path.is_dir()):
        os.utime(directory, ns=(0, 0))
    os.utime(output_dir, ns=(0, 0))


if __name__ == "__main__":
    main()
