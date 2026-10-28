# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""A transient Restic failure retries without losing metadata from the other source."""

from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.codex_titles_testing import index_record, snapshot_sqlite, transcript
from x.ai_session_archive.database import CodexTitleObservationRow, ResticSnapshotRow, SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticCommandError
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_retry_updates_observation_after_index_read_recovers(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    home = Path(env["HOME"])
    codex = home / ".codex"
    transcript(home)
    (codex / "session_index.jsonl").write_text(index_record("Index winner"))
    snapshot_sqlite(home, "SQLite fallback")
    _backup(env, Path(".codex"), Path(".local/state/ducktape/ai-session-backup-sqlite"), cwd=home)
    snapshot = repository.list_snapshots()[0]
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("host", "Host", repository, (HarnessRoot(Harness.CODEX, "/.codex"),))
    )

    original_dump = repository.dump_to

    def fail_index(snapshot_id: str, path: str, destination: BinaryIO) -> None:
        if path == "/.codex/session_index.jsonl":
            raise ResticCommandError(("dump", snapshot_id, "--", path), 1, b"temporary read failure")
        original_dump(snapshot_id, path, destination)

    with patch.object(repository, "dump_to", side_effect=fail_index):
        assert ingestor.ingest().snapshots_incomplete == 1
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "SQLite fallback"
        row = db.get(ResticSnapshotRow, ("host", snapshot.id))
        assert row is not None
        assert row.codex_metadata_status == "failed"
        observation = db.get(CodexTitleObservationRow, ("host", snapshot.id, "thread-one"))
        assert observation is not None
        assert observation.title_source == "sqlite_name"

    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "Index winner"
        row = db.get(ResticSnapshotRow, ("host", snapshot.id))
        assert row is not None
        assert row.codex_metadata_status == "complete"
        observation = db.get(CodexTitleObservationRow, ("host", snapshot.id, "thread-one"))
        assert observation is not None
        assert observation.title_source == "session_index"


if __name__ == "__main__":
    pytest_bazel.main()
