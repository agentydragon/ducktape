"""Regressions for archiving a live runner data mount."""

import json
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.runner_data_archive import capture


def _plan(tmp_path: Path, backups: list[dict[str, str]]) -> Path:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "archive": str(tmp_path / "data.tar"),
                "partial_archive": str(tmp_path / "data.tar.partial"),
                "manifest": str(tmp_path / "manifest.json"),
                "mounts": [{"source": str(mount), "destination": "/state"}],
                "backups": backups,
            }
        )
    )
    return plan


def _database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE event_entry (cursor INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO event_entry(cursor) VALUES (1)")


def _manifest(tmp_path: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((tmp_path / "manifest.json").read_text())
    return manifest


def test_archive_skips_only_successfully_backed_sqlite_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    backed = mount / "backed.sqlite"
    _database(backed)
    (mount / "unbacked.sqlite-journal").write_bytes(b"preserve unbacked sidecar")
    (mount / "durable.txt").write_text("preserve durable file")
    plan = _plan(
        tmp_path,
        [
            {"name": "backed", "source": str(backed), "destination": str(tmp_path / "backed-copy.sqlite")},
            {
                "name": "failed",
                "source": str(mount / "failed.sqlite"),
                "destination": str(tmp_path / "failed-copy.sqlite"),
            },
        ],
    )
    (mount / "failed.sqlite-journal").write_bytes(b"preserve failed backup sidecar")

    original_iterdir = Path.iterdir

    def vanish_during_listing(path: Path):
        if path != mount:
            return original_iterdir(path)
        sidecar = Path(f"{backed}-journal")
        sidecar.write_bytes(b"temporary journal")
        children = list(original_iterdir(path))
        sidecar.unlink()
        return iter(children)

    monkeypatch.setattr(Path, "iterdir", vanish_during_listing)
    assert capture(plan) == 0
    manifest = _manifest(tmp_path)
    assert manifest["archive"] == "complete"
    assert manifest["backups"]["backed"]["integrity_check"] == "ok"
    assert "error" in manifest["backups"]["failed"]
    with tarfile.open(tmp_path / "data.tar") as archive:
        names = set(archive.getnames())
    assert "state/backed.sqlite-journal" not in names
    assert "state/unbacked.sqlite-journal" in names
    assert "state/failed.sqlite-journal" in names
    assert "state/durable.txt" in names


@pytest.mark.parametrize("filename", ["durable.txt", "unbacked.sqlite-journal"])
def test_archive_still_fails_for_unbacked_disappearing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    plan = _plan(tmp_path, [])
    mount = tmp_path / "mount"
    original_iterdir = Path.iterdir

    def vanish_during_listing(path: Path):
        if path != mount:
            return original_iterdir(path)
        doomed = mount / filename
        doomed.write_bytes(b"present at listing")
        children = list(original_iterdir(path))
        doomed.unlink()
        return iter(children)

    monkeypatch.setattr(Path, "iterdir", vanish_during_listing)
    assert capture(plan) == 1
    manifest = _manifest(tmp_path)
    assert manifest["archive"] == "partial_or_failed"
    assert "FileNotFoundError" in manifest["archive_error"]
    assert not (tmp_path / "data.tar").exists()


if __name__ == "__main__":
    pytest_bazel.main()
