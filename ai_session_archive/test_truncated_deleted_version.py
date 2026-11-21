# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep :testing
from pathlib import Path

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ai_session_archive.database import SessionRow, SnapshotFileRow
from ai_session_archive.models import Harness
from ai_session_archive.queries import list_sessions
from ai_session_archive.source_paths import HarnessRoot
from ai_session_archive.testing import backup, new_ingestor, new_repository
from ai_session_archive.version_queries import list_events, list_versions

pytest_plugins = ["testing.postgres_fixtures", "ai_session_archive.testing"]


def test_truncated_and_deleted_file_keeps_prior_versions(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    main_path = claude_root / "projects" / "example-project" / "main-session.jsonl"
    main_path.parent.mkdir(parents=True)
    repeated = b'{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}\n'
    ending = b'{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":"ok"}}\n'
    main_path.write_bytes(repeated + repeated + ending)
    backup(env, claude_root)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = new_ingestor(
        factory, repository, HarnessRoot(Harness.CLAUDE, str(claude_root)), machine_id="archive-truncated-test"
    )
    report = ingestor.ingest()
    assert report.files_failed == 0
    main_path.write_bytes(repeated)
    backup(env, claude_root)
    truncation_snapshot = repository.list_snapshots()[-1]
    report = ingestor.ingest()
    assert report.files_failed == 0
    main_path.unlink()
    backup(env, claude_root)
    deletion_snapshot = repository.list_snapshots()[-1]
    report = ingestor.ingest()
    assert report.files_failed == 0

    with factory() as db:
        main = next(
            item
            for item in list_sessions(
                db, machine_id="archive-truncated-test", harness=Harness.CLAUDE, cursor=None, limit=100
            ).data
            if item.title == "hello"
        )
        versions = list_versions(db, session_id=main.id)
        assert len(versions) == 2
        original = next(version for version in versions if version.snapshot_id == initial_snapshot.id)
        original_events = list_events(db, session_id=main.id, version_id=original.id, cursor=None, limit=20)
        assert original_events is not None
        assert [event.ordinal for event in original_events.data] == [0, 1, 2]
        truncated = next(version for version in versions if version.snapshot_id == truncation_snapshot.id)
        events = list_events(db, session_id=main.id, version_id=truncated.id, cursor=None, limit=20)
        assert events is not None
        assert [event.ordinal for event in events.data] == [0]
        deletion = db.get(SnapshotFileRow, ("archive-truncated-test", deletion_snapshot.id, str(main_path)))
        assert deletion is not None
        assert deletion.status == "deleted"
        assert db.scalar(select(SessionRow).where(SessionRow.id == main.id)) is not None


if __name__ == "__main__":
    pytest_bazel.main()
