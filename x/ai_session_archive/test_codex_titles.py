# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""Codex names from captured metadata, including later renames and ambiguous IDs."""

from pathlib import Path

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.codex_titles_testing import snapshot_sqlite, transcript
from x.ai_session_archive.database import CodexTitleObservationRow, SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_codex_titles_backfill(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    home = Path(env["HOME"])
    codex = home / ".codex"
    transcript(home)
    _backup(env, Path(".codex"), cwd=home)
    first_snapshot = repository.list_snapshots()[0]
    assert repository.list_files(first_snapshot.id, "/.codex/session_index.jsonl") == []
    assert (
        repository.list_files(first_snapshot.id, "/.local/state/ducktape/ai-session-backup-sqlite/manifest.json") == []
    )
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("host", "Host", repository, (HarnessRoot(Harness.CODEX, "/.codex"),))
    )
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) is None

    # A partially captured index must not hide the good SQLite image.
    (codex / "session_index.jsonl").write_text('{"id":"unfinished"')
    snapshot_sqlite(home, "First name")
    _backup(env, Path(".codex"), Path(".local/state/ducktape/ai-session-backup-sqlite"), cwd=home)
    second_snapshot = repository.list_snapshots()[-1]
    report = ingestor.ingest()
    assert report.snapshots_incomplete == 0
    assert report.codex_metadata_partial == 1
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "First name"
        observation = db.get(CodexTitleObservationRow, ("host", second_snapshot.id, "thread-one"))
        assert observation is not None
        assert observation.title_source == "sqlite_name"
        assert observation.source_path == ("/.codex/sessions/2026/01/thread-one.jsonl")


if __name__ == "__main__":
    pytest_bazel.main()
