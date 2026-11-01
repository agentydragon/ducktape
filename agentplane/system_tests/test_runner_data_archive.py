"""Live-mount archive failures must retain accurate forensic provenance."""

import errno
import json
import sqlite3
import tarfile
from pathlib import Path

from agentplane.system_tests import runner_data_archive


def _plan(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "state"
    source.mkdir()
    (source / "payload.txt").write_text("retained evidence")
    journal_db = source / "journal.sqlite"
    database = sqlite3.connect(journal_db)
    try:
        database.execute("CREATE TABLE event_entry (cursor INTEGER PRIMARY KEY)")
        database.execute("INSERT INTO event_entry(cursor) VALUES (1)")
        database.commit()
    finally:
        database.close()
    archive = tmp_path / "runner-data.tar"
    partial = tmp_path / "runner-data.tar.partial"
    manifest = tmp_path / "archive-manifest.json"
    plan = tmp_path / "archive-plan.json"
    plan.write_text(
        json.dumps(
            {
                "archive": str(archive),
                "partial_archive": str(partial),
                "manifest": str(manifest),
                "mounts": [{"source": str(source), "destination": "/state"}],
                "backups": [
                    {"name": "session", "source": str(journal_db), "destination": str(tmp_path / "backup.sqlite")}
                ],
            }
        )
    )
    return plan, source, archive, manifest


def test_disappearing_sqlite_journal_restarts_the_whole_archive(tmp_path: Path, monkeypatch) -> None:
    plan, source, archive, manifest_path = _plan(tmp_path)
    journal = source / "journal.sqlite-journal"
    add = tarfile.TarFile.add
    failed = False

    def vanish_before_open(self, name, *args, **kwargs):
        nonlocal failed
        if Path(name) == source and not failed:
            journal.write_text("transient")
        if Path(name) == journal and not failed:
            failed = True
            journal.unlink()
        return add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", vanish_before_open)
    assert runner_data_archive.capture(plan) == 0
    manifest = json.loads(manifest_path.read_text())
    assert manifest["archive"] == "complete"
    assert manifest["backups"]["session"]["integrity_check"] == "ok"
    assert len(manifest["archive_attempts"]) == 2
    assert "FileNotFoundError" in manifest["archive_attempts"][0]["error"]
    assert Path(manifest["archive_attempts"][0]["partial_archive_path"]).exists()
    with tarfile.open(archive) as output:
        payload = output.extractfile("state/payload.txt")
        assert payload is not None
        assert payload.read() == b"retained evidence"
        assert "state/journal.sqlite-journal" not in output.getnames()


def test_persistent_journal_loss_stays_partial(tmp_path: Path, monkeypatch) -> None:
    plan, source, archive, manifest_path = _plan(tmp_path)
    journal = source / "journal.sqlite-journal"
    add = tarfile.TarFile.add

    def missing_journal(self, name, *args, **kwargs):
        if Path(name) == source:
            journal.write_text("transient")
        if Path(name) == journal:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(journal))
        return add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", missing_journal)
    assert runner_data_archive.capture(plan) == 1
    manifest = json.loads(manifest_path.read_text())
    assert manifest["archive"] == "partial_or_failed"
    assert len(manifest["archive_attempts"]) == 3
    assert all(Path(attempt["partial_archive_path"]).exists() for attempt in manifest["archive_attempts"])
    assert not archive.exists()


def test_missing_regular_file_does_not_retry(tmp_path: Path, monkeypatch) -> None:
    plan, source, archive, manifest_path = _plan(tmp_path)
    missing = source / "payload.txt"
    add = tarfile.TarFile.add

    def missing_regular_file(self, name, *args, **kwargs):
        if Path(name) == missing:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(missing))
        return add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", missing_regular_file)
    assert runner_data_archive.capture(plan) == 1
    manifest = json.loads(manifest_path.read_text())
    assert manifest["archive"] == "partial_or_failed"
    assert len(manifest["archive_attempts"]) == 1
    assert not archive.exists()


def test_unrelated_authored_journal_name_does_not_retry(tmp_path: Path, monkeypatch) -> None:
    plan, source, archive, manifest_path = _plan(tmp_path)
    authored = source / "notes-journal"
    authored.write_text("user evidence")
    add = tarfile.TarFile.add

    def vanish_before_open(self, name, *args, **kwargs):
        if Path(name) == authored:
            authored.unlink()
        return add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", vanish_before_open)
    assert runner_data_archive.capture(plan) == 1
    manifest = json.loads(manifest_path.read_text())
    assert manifest["archive"] == "partial_or_failed"
    assert len(manifest["archive_attempts"]) == 1
    assert not archive.exists()


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
