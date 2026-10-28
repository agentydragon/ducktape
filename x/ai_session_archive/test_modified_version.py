# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import os
from pathlib import Path

import pytest_bazel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import list_events, list_sessions, list_versions
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_changed_file_version_is_kept_with_same_metadata_diff(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    main_path = claude_root / "projects" / "example-project" / "main-session.jsonl"
    main_path.parent.mkdir(parents=True)
    repeated = b'{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}\n'
    ending_b = b'{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":"ok"}}\n'
    ending_c = ending_b.replace(b'"ok"', b'"no"')
    main_path.write_bytes(repeated + repeated + ending_b)
    _backup(env, claude_root)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="archive-modified-test",
        display_name="Archive modified test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    ingestor = SnapshotIngestor(factory, source)
    report = ingestor.ingest()
    assert report.files_failed == 0

    original_stat = main_path.stat()
    main_path.write_bytes(repeated + repeated + ending_c)
    os.utime(main_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    _backup(env, claude_root)
    snapshots = repository.list_snapshots()
    changed_snapshot = next(snapshot for snapshot in snapshots if snapshot.id != initial_snapshot.id)
    assert changed_snapshot.parent_id == initial_snapshot.id
    changes = repository.diff_snapshots(initial_snapshot.id, changed_snapshot.id)
    assert any(change.path.endswith("/main-session.jsonl") and change.modifier == "M" for change in changes), [
        (change.path, change.modifier) for change in changes
    ]
    report = ingestor.ingest()
    assert report.files_failed == 0

    with factory() as db:
        main = next(
            item
            for item in list_sessions(
                db, machine_id="archive-modified-test", harness=Harness.CLAUDE, cursor=None, limit=100
            ).data
            if item.title == "hello"
        )
        versions = list_versions(db, session_id=main.id)
        assert len(versions) == 2
        first = next(version for version in versions if version.snapshot_id == initial_snapshot.id)
        changed = next(version for version in versions if version.snapshot_id == changed_snapshot.id)
        first_events = list_events(db, session_id=main.id, version_id=first.id, cursor=None, limit=20)
        changed_events = list_events(db, session_id=main.id, version_id=changed.id, cursor=None, limit=20)
        assert first_events is not None
        assert changed_events is not None
        assert [event.ordinal for event in changed_events.data] == [0, 1, 2]
        assert changed_events.data[0].raw_base64 == changed_events.data[1].raw_base64
        assert first_events.data[2].raw_base64 != changed_events.data[2].raw_base64
        changed_row = db.get(SnapshotFileRow, ("archive-modified-test", changed_snapshot.id, str(main_path)))
        assert changed_row is not None
        assert changed_row.change_kind == "M"
        changed_snapshot_row = db.get(ResticSnapshotRow, ("archive-modified-test", changed_snapshot.id))
        assert changed_snapshot_row is not None
        assert changed_snapshot_row.comparison_snapshot_id == initial_snapshot.id


if __name__ == "__main__":
    pytest_bazel.main()
