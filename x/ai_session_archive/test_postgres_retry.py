# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import BinaryIO

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import FileVersionRow, RecordPayloadRow, ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_postgres_file_transaction_rolls_back_and_retries(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    _backup(env, claude_root)
    source = ArchiveSource(
        machine_id="transaction-test",
        display_name="Transaction test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(factory, source)

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE FUNCTION reject_archive_record_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'injected archive record failure';
            END;
            $$
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_archive_record_insert
            BEFORE INSERT ON file_version_records
            FOR EACH ROW EXECUTE FUNCTION reject_archive_record_insert()
            """
        )

    failed_report = ingestor.ingest()
    assert failed_report.files_failed == 1
    assert failed_report.snapshots_incomplete == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 0
        # The completed staging batch survives a failed publication transaction.
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 1

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_archive_record_insert ON file_version_records")
        connection.exec_driver_sql("DROP FUNCTION reject_archive_record_insert()")

    retried_report = ingestor.ingest()
    assert retried_report.files_failed == 0
    assert retried_report.snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 1
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 1


def test_inventory_rechecks_diff_parent_after_restic_read(postgres_engine: Engine) -> None:
    parent = ResticSnapshot(
        id="parent",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        parent_id=None,
        tree_id="parent-tree",
        paths=("/home/archive/.claude",),
    )
    child = ResticSnapshot(
        id="child",
        captured_at=datetime(2026, 1, 2, tzinfo=UTC),
        parent_id=parent.id,
        tree_id="child-tree",
        paths=parent.paths,
    )
    full_path = "/home/archive/.claude/projects/project/session.jsonl"
    diff_only_path = "/home/archive/.claude/projects/project/stale-diff.jsonl"
    factory = sessionmaker(postgres_engine, expire_on_commit=False)

    class ParentChangingRepository(ResticRepository):
        diff_calls = 0
        list_calls = 0

        def __init__(self) -> None:
            super().__init__(binary="unused", environment={})

        def diff_snapshots(self, before_snapshot_id: str, after_snapshot_id: str) -> list[ResticChange]:
            self.diff_calls += 1
            assert (before_snapshot_id, after_snapshot_id) == (parent.id, child.id)
            with factory() as db, db.begin():
                parent_row = db.get(ResticSnapshotRow, ("parent-change-test", parent.id))
                assert parent_row is not None
                parent_row.processing_status = "failed"
            return [ResticChange(path=diff_only_path, modifier="+")]

        def list_files(self, snapshot_id: str, path: str | None = None) -> list[ResticFile]:
            self.list_calls += 1
            assert (snapshot_id, path) == (child.id, None)
            return [ResticFile(path=full_path, file_type="file", byte_size=12, modified_at=None)]

    repository = ParentChangingRepository()
    source = ArchiveSource(
        machine_id="parent-change-test",
        display_name="Parent change test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, "/home/archive/.claude"),),
    )
    ingestor = SnapshotIngestor(factory, source)
    ingestor._ensure_source()
    ingestor._discover_snapshots([parent, child])
    with factory() as db, db.begin():
        parent_row = db.get(ResticSnapshotRow, ("parent-change-test", parent.id))
        assert parent_row is not None
        parent_row.inventory_status = "complete"
        parent_row.processing_status = "complete"

    ingestor._prepare_snapshot(child, {parent.id, child.id})

    with factory() as db:
        child_row = db.get(ResticSnapshotRow, ("parent-change-test", child.id))
        assert child_row is not None
        assert child_row.inventory_status == "complete"
        assert child_row.comparison_snapshot_id is None
        files = list(
            db.scalars(
                select(SnapshotFileRow).where(
                    SnapshotFileRow.machine_id == "parent-change-test", SnapshotFileRow.snapshot_id == child.id
                )
            )
        )
    assert repository.diff_calls == 1
    assert repository.list_calls == 1
    assert [(row.path, row.change_kind) for row in files] == [(full_path, "inventory")]


def test_concurrent_import_does_not_hold_file_lock_during_restic_dump(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    _backup(env, claude_root)
    first_dump_started = Event()
    release_first_dump = Event()
    second_finished = Event()
    dump_calls = 0
    dump_calls_lock = Lock()

    class BlockingResticRepository(ResticRepository):
        def __init__(self, repository: ResticRepository) -> None:
            super().__init__(binary=repository.binary, environment=repository.environment)

        def dump_to(self, snapshot_id: str, path: str, destination: BinaryIO) -> None:
            nonlocal dump_calls
            with dump_calls_lock:
                dump_calls += 1
                call_number = dump_calls
            if call_number == 1:
                first_dump_started.set()
                if not release_first_dump.wait(timeout=30):
                    raise TimeoutError("test did not release the first Restic dump")
            super().dump_to(snapshot_id, path, destination)

    blocking_repository = BlockingResticRepository(repository)
    source = ArchiveSource(
        machine_id="concurrent-import-test",
        display_name="Concurrent import test host",
        restic=blocking_repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(factory, source)
    executor = ThreadPoolExecutor(max_workers=2)
    first = executor.submit(ingestor.ingest)
    second = None
    try:
        assert first_dump_started.wait(timeout=15)
        with factory() as db:
            file_row = db.scalar(select(SnapshotFileRow))
            assert file_row is not None
            assert file_row.status == "pending"
            assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 0

        second = executor.submit(ingestor.ingest)
        second.add_done_callback(lambda _: second_finished.set())
        completed_while_first_dump_blocked = second_finished.wait(timeout=15)
    finally:
        release_first_dump.set()
        executor.shutdown(wait=True)

    assert second is not None
    second_report = second.result()
    first_report = first.result()
    assert completed_while_first_dump_blocked
    assert first_report.files_failed == second_report.files_failed == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 1
        file_row = db.scalar(select(SnapshotFileRow))
        assert file_row is not None
        assert file_row.status in ("complete", "partial")


if __name__ == "__main__":
    pytest_bazel.main()
