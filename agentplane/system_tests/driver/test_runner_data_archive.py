"""Regressions for archiving a live runner data mount."""

import json
import socket
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.driver import runner_data_archive


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
    assert runner_data_archive.capture(plan) == 0
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


@pytest.mark.parametrize("filename", ["durable.txt", "notes.sqlite", "unbacked.sqlite-journal"])
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
    assert runner_data_archive.capture(plan) == 1
    manifest = _manifest(tmp_path)
    assert manifest["archive"] == "partial_or_failed"
    assert "FileNotFoundError" in manifest["archive_error"]
    assert not (tmp_path / "data.tar").exists()


def test_archive_preserves_tar_socket_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, [])
    mount = tmp_path / "mount"
    (mount / "durable.txt").write_text("preserve durable file")
    # A relative bind avoids the Unix-domain socket path length limit in Bazel tempdirs.
    monkeypatch.chdir(mount)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("harness.sock")
        assert runner_data_archive.capture(plan) == 0
    with tarfile.open(tmp_path / "data.tar") as archive:
        contents = archive.extractfile("state/durable.txt")
        assert contents is not None
        assert contents.read() == b"preserve durable file"
        assert "state/harness.sock" not in archive.getnames()


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_archive_snapshots_discovered_harness_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal_mode: str
) -> None:
    plan = _plan(tmp_path, [])
    mount = tmp_path / "mount"
    database = mount / "thread_history_1.sqlite"
    with sqlite3.connect(database) as writer:
        writer.execute(f"PRAGMA journal_mode={journal_mode}")
        writer.execute("CREATE TABLE history (message TEXT)")
        writer.execute("INSERT INTO history VALUES ('committed')")
        writer.commit()
        writer.execute("INSERT INTO history VALUES ('uncommitted')")
        database.chmod(0o640)
        metadata = database.stat()
        (mount / "latest.sqlite").symlink_to(database.name)
        original_iterdir = Path.iterdir

        def rollback_after_listing(path: Path):
            children = list(original_iterdir(path))
            if path == mount and journal_mode == "DELETE":
                assert Path(f"{database}-journal") in children
                # Real SQLite removes its rollback journal between listing and tar stat.
                writer.rollback()
            return iter(children)

        monkeypatch.setattr(Path, "iterdir", rollback_after_listing)
        assert runner_data_archive.capture(plan) == 0
        writer.rollback()
        writer.execute("INSERT INTO history VALUES ('after capture')")
        writer.commit()
    with tarfile.open(tmp_path / "data.tar") as archive:
        member = archive.getmember("state/thread_history_1.sqlite")
        assert member.mode == 0o640
        assert member.mtime == metadata.st_mtime
        link = archive.getmember("state/latest.sqlite")
        assert link.issym()
        assert link.linkname == database.name
        assert set(archive.getnames()) == {"state", member.name, link.name}
        contents = archive.extractfile(member)
        assert contents is not None
        snapshot = tmp_path / "restored.sqlite"
        snapshot.write_bytes(contents.read())
    with sqlite3.connect(snapshot) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT message FROM history").fetchall() == [("committed",)]


def test_archive_fails_when_discovered_sqlite_backup_fails(tmp_path: Path) -> None:
    plan = _plan(tmp_path, [])
    (tmp_path / "mount" / "broken.sqlite").write_bytes(b"SQLite format 3\0" + bytes(100))
    assert runner_data_archive.capture(plan) == 1
    manifest = _manifest(tmp_path)
    assert manifest["archive"] == "partial_or_failed"
    assert "DatabaseError" in manifest["archive_error"]
    assert not (tmp_path / "data.tar").exists()


def test_archive_fails_when_sqlite_identity_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, [])
    database = tmp_path / "mount" / "history.sqlite"
    _database(database)
    original_backup = runner_data_archive._backup

    def replace_after_backup(source: Path, destination: Path, *, journal: bool) -> dict[str, Any]:
        result = original_backup(source, destination, journal=journal)
        source.rename(tmp_path / "original.sqlite")
        _database(source)
        return result

    monkeypatch.setattr(runner_data_archive, "_backup", replace_after_backup)
    assert runner_data_archive.capture(plan) == 1
    assert _manifest(tmp_path)["archive"] == "partial_or_failed"
    assert not (tmp_path / "data.tar").exists()


if __name__ == "__main__":
    pytest_bazel.main()
