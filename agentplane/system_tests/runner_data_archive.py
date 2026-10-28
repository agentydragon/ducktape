"""Archive a runner's writable mounts and make consistent SQLite backups for private test artifacts."""

import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from typing import Any


def _backup(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    source_db = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=5)
    target_db = sqlite3.connect(partial, timeout=5)
    try:
        source_db.backup(target_db, pages=256, sleep=0.01)
        integrity = target_db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check: {integrity}")
        event_count = target_db.execute("SELECT count(*) FROM event_entry").fetchone()[0]
        first_cursor = target_db.execute("SELECT min(cursor) FROM event_entry").fetchone()[0]
        last_cursor = target_db.execute("SELECT max(cursor) FROM event_entry").fetchone()[0]
        target_db.commit()
    finally:
        target_db.close()
        source_db.close()
    partial.replace(destination)
    return {
        "path": str(destination),
        "integrity_check": integrity,
        "event_count": event_count,
        "first_cursor": first_cursor,
        "last_cursor": last_cursor,
    }


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
            manifest["backups"][name] = _backup(Path(item["source"]), Path(item["destination"]))
        except Exception as error:
            manifest["backups"][name] = {"error": f"{type(error).__name__}: {error}"}
        persist()

    archive = Path(plan["archive"])
    partial_archive = Path(plan["partial_archive"])
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(partial_archive, "w") as output:
            for mount in plan["mounts"]:
                source = Path(mount["source"])
                if not source.exists():
                    raise FileNotFoundError(source)
                output.add(source, arcname=mount["destination"].lstrip("/"), recursive=True)
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
