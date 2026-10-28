# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""Codex renames win over older observations and SQLite auto titles."""

from pathlib import Path

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.codex_titles_testing import index_record, snapshot_sqlite, transcript
from x.ai_session_archive.database import ResticSnapshotRow, SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_codex_title_rename_order_and_partial_index(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    home = Path(env["HOME"])
    codex = home / ".codex"
    transcript(home)
    (codex / "session_index.jsonl").write_text(index_record("First name"))
    snapshot_sqlite(home, "Different SQLite title")
    _backup(env, Path(".codex"), Path(".local/state/ducktape/ai-session-backup-sqlite"), cwd=home)
    first_snapshot = repository.list_snapshots()[0]
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("host", "Host", repository, (HarnessRoot(Harness.CODEX, "/.codex"),))
    )
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "First name"

    with (codex / "session_index.jsonl").open("a") as index:
        index.write(index_record("Renamed", timestamp="2026-01-02T00:00:00Z"))
        index.write('{"id":"unfinished"')
    snapshot_sqlite(home, "Renamed")
    _backup(env, Path(".codex"), Path(".local/state/ducktape/ai-session-backup-sqlite"), cwd=home)
    report = ingestor.ingest()
    assert report.snapshots_incomplete == 0
    assert report.codex_metadata_partial == 1
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "Renamed"
        first = db.get(ResticSnapshotRow, ("host", first_snapshot.id))
        assert first is not None
        first.codex_metadata_status = "pending"
        db.commit()
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "Renamed"


if __name__ == "__main__":
    pytest_bazel.main()
