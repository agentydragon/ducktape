"""Archive a runner's writable mounts and make consistent SQLite backups for private test artifacts."""

import json
import sqlite3
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


def _backup(source: Path, destination: Path, *, journal: bool) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    with (
        closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=5)) as source_db,
        closing(sqlite3.connect(partial, timeout=5)) as target_db,
    ):
        source_db.backup(target_db, pages=256, sleep=0.01)
        integrity = target_db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check: {integrity}")
        result: dict[str, Any] = {"path": str(destination), "integrity_check": integrity}
        if journal:
            result.update(
                event_count=target_db.execute("SELECT count(*) FROM event_entry").fetchone()[0],
                first_cursor=target_db.execute("SELECT min(cursor) FROM event_entry").fetchone()[0],
                last_cursor=target_db.execute("SELECT max(cursor) FROM event_entry").fetchone()[0],
            )
        target_db.commit()
    partial.replace(destination)
    return result


def _add_tree(output: tarfile.TarFile, source: Path, arcname: str, excluded: set[Path], backup_dir: Path) -> None:
    if source in excluded:
        return
    member = output.gettarinfo(source, arcname=arcname)
    if member is None:
        # Match tarfile.add: sockets and other unsupported types have no tar member.
        return
    if member.isfile():
        identity = source.stat(follow_symlinks=False)
        with source.open("rb") as contents:
            sqlite = contents.read(16) == b"SQLite format 3\0"
        if sqlite:
            # Harness databases are not all session journals. Snapshot their committed
            # contents before excluding sidecars that can vanish during a live walk.
            with TemporaryDirectory(dir=backup_dir) as directory:
                snapshot = Path(directory) / "snapshot.sqlite"
                _backup(source, snapshot, journal=False)
                current = source.stat(follow_symlinks=False)
                if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                    raise RuntimeError(f"SQLite database changed identity during backup: {source}")
                member.size = snapshot.stat().st_size
                with snapshot.open("rb") as contents:
                    output.addfile(member, contents)
            excluded.update(Path(f"{source}{suffix}") for suffix in ("-journal", "-wal", "-shm"))
        else:
            with source.open("rb") as contents:
                output.addfile(member, contents)
    else:
        output.addfile(member)
    if member.isdir():
        for child in sorted(source.iterdir()):
            _add_tree(output, child, f"{arcname}/{child.name}", excluded, backup_dir)


def capture(plan_path: Path) -> int:
    plan = json.loads(plan_path.read_text())
    manifest: dict[str, Any] = {"archive": "pending", "backups": {}}
    manifest_path = Path(plan["manifest"])

    def persist() -> None:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    persist()
    for item in plan["backups"]:
        name = item["name"]
        try:
            manifest["backups"][name] = _backup(Path(item["source"]), Path(item["destination"]), journal=True)
        except Exception as error:
            manifest["backups"][name] = {"error": f"{type(error).__name__}: {error}"}
        persist()

    # The consistent backup contains the database contents. Its live SQLite sidecars can
    # appear or disappear while the archive walks a mounted directory.
    backed_sidecars = {
        Path(f"{item['source']}{suffix}")
        for item in plan["backups"]
        if "path" in manifest["backups"][item["name"]]
        for suffix in ("-journal", "-wal", "-shm")
    }
    archive = Path(plan["archive"])
    partial_archive = Path(plan["partial_archive"])
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(partial_archive, "w") as output:
            for mount in plan["mounts"]:
                source = Path(mount["source"])
                if not source.exists():
                    raise FileNotFoundError(source)
                _add_tree(output, source, mount["destination"].lstrip("/"), backed_sidecars, archive.parent)
        partial_archive.replace(archive)
        manifest["archive"] = "complete"
        manifest["archive_path"] = str(archive)
    except Exception as error:
        manifest["archive"] = "partial_or_failed"
        manifest["archive_error"] = f"{type(error).__name__}: {error}"

    persist()
    return 0 if manifest["archive"] == "complete" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: runner_data_archive.py PLAN.json")
    raise SystemExit(capture(Path(sys.argv[1])))
