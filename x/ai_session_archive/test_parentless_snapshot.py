# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from pathlib import Path

import pytest_bazel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_parentless_restic_snapshot_uses_full_inventory(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"content":"original"}}\n')
    _backup(env, claude_root)
    first_snapshot = repository.list_snapshots()[0]
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="parentless-test",
        display_name="Parentless test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    ingestor = SnapshotIngestor(factory, source)
    ingestor.ingest()

    transcript.write_bytes(b'{"type":"user","message":{"content":"forced snapshot"}}\n')
    _backup(env, claude_root, force=True)
    second_snapshot = next(snapshot for snapshot in repository.list_snapshots() if snapshot.id != first_snapshot.id)
    assert second_snapshot.parent_id is None
    report = ingestor.ingest()
    assert report.files_failed == 0
    assert report.snapshots_incomplete == 0
    with factory() as db:
        snapshot_row = db.get(ResticSnapshotRow, ("parentless-test", second_snapshot.id))
        assert snapshot_row is not None
        assert snapshot_row.comparison_snapshot_id is None
        file_row = db.get(SnapshotFileRow, ("parentless-test", second_snapshot.id, str(transcript)))
        assert file_row is not None
        assert file_row.change_kind == "inventory"
        assert file_row.status == "complete"


if __name__ == "__main__":
    pytest_bazel.main()
