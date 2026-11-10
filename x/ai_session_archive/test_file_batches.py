"""Durable file staging must never change the published read API before commit."""

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
import cProfile
import hashlib
import pstats
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any, BinaryIO
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from x.ai_session_archive import ingest
from x.ai_session_archive.database import (
    FileVersionImportRecordRow,
    FileVersionImportRow,
    FileVersionRecordRow,
    FileVersionRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
    UsageObservationRow,
)
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, IngestReport, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import get_version, list_events, list_sessions, list_versions
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]
PATH = "/.claude/projects/project/session.jsonl"
SNAPSHOT = ResticSnapshot(
    id="snapshot", captured_at=datetime(2026, 1, 1, tzinfo=UTC), parent_id=None, tree_id="tree", paths=("/.claude",)
)


class MemoryRepository(ResticRepository):
    def __init__(self, contents: bytes):
        super().__init__(binary="unused", environment={})
        self.contents = {SNAPSHOT.id: contents}
        self.snapshots = [SNAPSHOT]

    def list_snapshots(self) -> list[ResticSnapshot]:
        return list(self.snapshots)

    def list_files(self, snapshot_id: str, path: str | None = None) -> list[ResticFile]:
        return [ResticFile(path=PATH, file_type="file", byte_size=len(self.contents[snapshot_id]), modified_at=None)]

    def diff_snapshots(self, before_id: str, after_id: str) -> list[ResticChange]:
        return [ResticChange(path=PATH, modifier="M")]

    def dump_to(self, snapshot_id: str, path: str, destination: BinaryIO) -> None:
        destination.write(self.contents[snapshot_id])


def _ingestor(engine: Engine, contents: bytes, machine_id: str = "batch-test") -> SnapshotIngestor:
    return SnapshotIngestor(
        sessionmaker(engine, expire_on_commit=False),
        ArchiveSource(
            machine_id=machine_id,
            display_name="Batch test",
            restic=MemoryRepository(contents),
            roots=(HarnessRoot(Harness.CLAUDE, "/.claude"),),
        ),
    )


def _contents() -> bytes:
    return (
        b'{"type":"user","message":{"content":"original"}}\r\n'
        + b'{"type":"assistant","message":{"usage":{"input_tokens":3}}}\n' * 1000
        + b"\xff\nnull\n{"
    )


class SimulatedCrash(BaseException):
    pass


def test_committed_batch_survives_crash_without_publishing(postgres_engine: Engine) -> None:
    contents = _contents()
    importer = _ingestor(postgres_engine, contents)
    store_batch = importer._store_record_batch
    read_batch = ingest._read_record_batch
    factory = sessionmaker(postgres_engine)
    committed_sizes = []

    def parse_without_transaction(*args: Any, **kwargs: Any) -> list[ingest._ParsedRecord]:
        assert isinstance(postgres_engine.pool, QueuePool)
        assert postgres_engine.pool.checkedout() == 0
        return read_batch(*args, **kwargs)

    def crash_after_second_batch(*args: Any, **kwargs: Any) -> None:
        store_batch(*args, **kwargs)
        with factory() as db:
            stage = db.scalar(select(FileVersionImportRow))
            assert stage is not None
            committed_sizes.append(stage.imported_record_count)
            session = db.scalar(select(SessionRow))
            assert session is not None
            assert session.title is None
            summary = list_sessions(db, machine_id="batch-test", harness=Harness.CLAUDE, cursor=None, limit=10).data[0]
            assert summary.version_count == 0
            assert summary.latest_version_id is None
            assert list_versions(db, session_id=session.id) == []
            assert get_version(db, session_id=session.id, version_id=stage.id) is None
            assert list_events(db, session_id=session.id, version_id=stage.id, cursor=None, limit=10) is None
            assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 0
            assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 2
            assert db.scalar(select(func.count()).select_from(UsageObservationRow)) == 1
            file = db.get(SnapshotFileRow, ("batch-test", SNAPSHOT.id, PATH))
            assert file is not None
            assert file.file_version_id is None
            assert file.status == "pending"
        if len(committed_sizes) == 2:
            raise SimulatedCrash

    with (
        patch.object(importer, "_store_record_batch", side_effect=crash_after_second_batch),
        patch.object(ingest, "_read_record_batch", side_effect=parse_without_transaction),
        pytest.raises(SimulatedCrash),
    ):
        importer.ingest()
    assert committed_sizes == [500, 1000]
    assert _ingestor(postgres_engine, contents).ingest().files_partial == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionImportRow)) == 0
        assert db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)) == 0
        version = db.scalar(select(FileVersionRow))
        assert version is not None
        assert version.record_count == 1004
        assert version.parse_issue_count == 2
        session = db.get(SessionRow, version.session_id)
        assert session is not None
        assert session.title == "original"
        events = []
        cursor = None
        while True:
            page = list_events(db, session_id=version.session_id, version_id=version.id, cursor=cursor, limit=500)
            assert page is not None
            events.extend(page.data)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert b"".join(base64.b64decode(event.raw_base64) for event in events) == contents
        assert db.scalar(select(func.count()).select_from(FileVersionRecordRow)) == 1004
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 5


@pytest.mark.parametrize("same_machine", [True, False])
def test_concurrent_importers_share_committed_batches(postgres_engine: Engine, same_machine: bool) -> None:
    contents = _contents()
    importers = [
        _ingestor(postgres_engine, contents),
        _ingestor(postgres_engine, contents, machine_id="batch-test" if same_machine else "other-machine"),
    ]
    barrier = Barrier(2)

    def run(importer: SnapshotIngestor) -> IngestReport:
        store_batch = importer._store_record_batch
        first_batch = True

        def synchronize(*args: Any, **kwargs: Any) -> None:
            nonlocal first_batch
            if first_batch:
                first_batch = False
                barrier.wait(timeout=15)
            store_batch(*args, **kwargs)

        with patch.object(importer, "_store_record_batch", side_effect=synchronize):
            return importer.ingest()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(run, importers))
    assert all(report.files_failed == 0 and report.files_partial == 1 for report in reports)
    with sessionmaker(postgres_engine)() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == (1 if same_machine else 2)
        assert db.scalar(select(func.count()).select_from(FileVersionRecordRow)) == (1004 if same_machine else 2008)
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 5
        assert db.scalar(select(func.count()).select_from(FileVersionImportRow)) == 0
        assert db.scalar(select(func.count()).select_from(UsageObservationRow)) == 1


def test_failed_batch_preserves_current_capture_then_retries(postgres_engine: Engine) -> None:
    old_contents = b'{"type":"user","message":{"content":"old title"}}\n'
    importer = _ingestor(postgres_engine, old_contents)
    assert importer.ingest().files_complete == 1
    repository = importer._source.restic
    assert isinstance(repository, MemoryRepository)
    next_snapshot = replace(SNAPSHOT, id="next", captured_at=SNAPSHOT.captured_at + timedelta(days=1))
    repository.snapshots.append(next_snapshot)
    repository.contents[next_snapshot.id] = _contents()
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE FUNCTION reject_second_batch() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.ordinal = 750 THEN
                RAISE EXCEPTION 'injected second batch failure';
              END IF;
              RETURN NEW;
            END;
            $$
        """)
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_second_batch BEFORE INSERT ON file_version_import_records
            FOR EACH ROW EXECUTE FUNCTION reject_second_batch()
        """)
    assert importer.ingest().files_failed == 1
    with sessionmaker(postgres_engine)() as db:
        stage = db.scalar(select(FileVersionImportRow))
        assert stage is not None
        assert stage.imported_record_count == 500
        assert db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)) == 500
        current = list_sessions(db, machine_id="batch-test", harness=Harness.CLAUDE, cursor=None, limit=10).data[0]
        assert current.title == "old title"
        assert current.version_count == 1
        assert current.latest_version_record_count == 1
        assert len(list_versions(db, session_id=current.id)) == 1
        assert get_version(db, session_id=current.id, version_id=stage.id) is None
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_second_batch ON file_version_import_records")
        connection.exec_driver_sql("DROP FUNCTION reject_second_batch()")
    assert importer.ingest().files_partial == 1
    with sessionmaker(postgres_engine)() as db:
        current = list_sessions(db, machine_id="batch-test", harness=Harness.CLAUDE, cursor=None, limit=10).data[0]
        assert current.title == "original"
        assert current.version_count == 2
        assert current.latest_version_record_count == 1004
        assert db.scalar(select(func.count()).select_from(FileVersionImportRow)) == 0


def test_failed_inventory_cannot_overwrite_concurrent_success(postgres_engine: Engine) -> None:
    importer = _ingestor(postgres_engine, _contents())
    winner = _ingestor(postgres_engine, _contents())
    importer._ensure_source()
    importer._discover_snapshots([SNAPSHOT])
    mark_failed = importer._mark_inventory_failed

    def finish_inventory_before_failure_marker(snapshot: ResticSnapshot, error: Exception) -> None:
        winner._prepare_snapshot(snapshot, {snapshot.id})
        mark_failed(snapshot, error)

    with (
        patch.object(importer._source.restic, "list_files", side_effect=RuntimeError("injected inventory failure")),
        patch.object(importer, "_mark_inventory_failed", side_effect=finish_inventory_before_failure_marker),
    ):
        importer._prepare_snapshot(SNAPSHOT, {SNAPSHOT.id})
    with sessionmaker(postgres_engine)() as db:
        snapshot = db.get(ResticSnapshotRow, ("batch-test", SNAPSHOT.id))
        assert snapshot is not None
        assert snapshot.inventory_status == "complete"
        assert snapshot.processing_status == "pending"
        assert snapshot.inventory_error is None


def test_large_checkpoint_and_growing_publication(postgres_engine: Engine) -> None:
    record_count = 50_000
    contents = b'{"type":"assistant"}\n' * record_count
    importer = _ingestor(postgres_engine, contents)
    publish = importer._publish_version
    profile = cProfile.Profile()
    output = undeclared_outputs_dir()

    def profile_publication(*args: Any, **kwargs: Any) -> None:
        profile.runcall(publish, *args, **kwargs)

    with patch.object(importer, "_publish_version", side_effect=profile_publication):
        assert importer.ingest().files_complete == 1
    profile.dump_stats(output / "publication-50000.prof")
    with (output / "publication-50000.txt").open("w") as stream:
        pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(30)
    repository = importer._source.restic
    assert isinstance(repository, MemoryRepository)
    growing = replace(
        SNAPSHOT, id="growing", parent_id=SNAPSHOT.id, captured_at=SNAPSHOT.captured_at + timedelta(days=1)
    )
    repository.snapshots.append(growing)
    repository.contents[growing.id] = contents + b'{"type":"user","message":{"content":"new"}}\n'
    store_batch = importer._store_record_batch
    stage_sizes = []

    def inspect_staging(*args: Any, **kwargs: Any) -> None:
        store_batch(*args, **kwargs)
        with sessionmaker(postgres_engine)() as db:
            stage_sizes.append(db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)))

    profile.clear()
    with (
        patch.object(importer, "_publish_version", side_effect=profile_publication),
        patch.object(importer, "_store_record_batch", side_effect=inspect_staging),
    ):
        assert importer.ingest().files_complete == 2
    assert stage_sizes == [0] * 100 + [1]
    with sessionmaker(postgres_engine)() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRecordRow)) == record_count + 1
        assert db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)) == 0
        row = db.scalar(select(FileVersionRow).where(FileVersionRow.first_snapshot_id == growing.id))
        assert row is not None
        assert row.shared_prefix_record_count == record_count
    profile.dump_stats(output / "publication-growing-50001.prof")
    with (output / "publication-growing-50001.txt").open("w") as stream:
        pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(30)


def test_prefix_retry_and_changed_parent_reconstruct_checkpoint(postgres_engine: Engine) -> None:
    contents = b'{"type":"user","message":{"content":"original"}}\r\n' + b'{"type":"assistant"}\n' * 1000
    importer = _ingestor(postgres_engine, contents)
    assert importer.ingest().files_complete == 1
    repository = importer._source.restic
    assert isinstance(repository, MemoryRepository)
    ancestor = replace(
        SNAPSHOT, id="ancestor", parent_id=SNAPSHOT.id, captured_at=SNAPSHOT.captured_at + timedelta(days=1)
    )
    repository.snapshots.append(ancestor)
    lines = contents.splitlines(keepends=True)
    lines[250] = b'{"type":"assistant","message":"changed"}\n'
    contents = b"".join(lines)
    repository.contents[ancestor.id] = contents
    assert importer.ingest().files_complete == 2
    alternate = replace(SNAPSHOT, id="alternate", captured_at=SNAPSHOT.captured_at + timedelta(days=2))
    repository.snapshots.append(alternate)
    repository.contents[alternate.id] = b'{"type":"user","message":{"content":"alternate"}}\n'
    assert importer.ingest().files_complete == 3
    current = replace(
        SNAPSHOT, id="current", parent_id=ancestor.id, captured_at=SNAPSHOT.captured_at + timedelta(days=3)
    )
    repository.snapshots.append(current)
    repository.contents[current.id] = contents + b'{"type":"custom-title","customTitle":"new title"}\n'
    store_batch = importer._store_record_batch

    def crash_after_prefix(*args: Any, **kwargs: Any) -> None:
        store_batch(*args, **kwargs)
        raise SimulatedCrash

    with patch.object(importer, "_store_record_batch", side_effect=crash_after_prefix), pytest.raises(SimulatedCrash):
        importer.ingest()
    with sessionmaker(postgres_engine)() as db:
        stage = db.scalar(select(FileVersionImportRow))
        assert stage is not None
        assert stage.imported_record_count == stage.shared_prefix_record_count == 500
        assert db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)) == 0
    publish = importer._publish_version

    def change_ancestry_before_publication(*args: Any, **kwargs: Any) -> None:
        # Exercise publication's revalidation after snapshot ancestry is repaired independently.
        with sessionmaker(postgres_engine)() as db, db.begin():
            snapshot = db.get(ResticSnapshotRow, ("batch-test", current.id))
            assert snapshot is not None
            snapshot.comparison_snapshot_id = alternate.id
        publish(*args, **kwargs)

    with patch.object(importer, "_publish_version", side_effect=change_ancestry_before_publication):
        assert importer.ingest().files_complete == 4
    with sessionmaker(postgres_engine)() as db:
        version = db.scalar(select(FileVersionRow).where(FileVersionRow.first_snapshot_id == current.id))
        assert version is not None
        assert version.is_checkpoint
        assert version.parent_version_id is None
        assert version.shared_prefix_record_count == 0
        events = []
        cursor = None
        while True:
            page = list_events(db, session_id=version.session_id, version_id=version.id, cursor=cursor, limit=500)
            assert page is not None
            events.extend(page.data)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert b"".join(base64.b64decode(event.raw_base64) for event in events) == repository.contents[current.id]
        offset = 0
        for event, raw_line in zip(events, repository.contents[current.id].splitlines(keepends=True), strict=True):
            assert event.byte_offset == offset
            offset += len(raw_line)


def test_payload_collision_rejects_version_without_changing_evidence(postgres_engine: Engine) -> None:
    contents = b'{"type":"user","message":{"content":"source"}}\n'
    payload_id = uuid4()
    with sessionmaker(postgres_engine)() as db, db.begin():
        db.add(
            RecordPayloadRow(
                id=payload_id,
                sha256=hashlib.sha256(contents).hexdigest(),
                parser_version=ingest.PARSER_VERSION,
                raw_bytes=b"different bytes",
                parse_error=None,
                timestamp=None,
                kind=None,
            )
        )
    assert _ingestor(postgres_engine, contents).ingest().files_failed == 1
    with sessionmaker(postgres_engine)() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 0
        payload = db.get(RecordPayloadRow, payload_id)
        assert payload is not None
        assert payload.raw_bytes == b"different bytes"
        file = db.get(SnapshotFileRow, ("batch-test", SNAPSHOT.id, PATH))
        assert file is not None
        assert file.error is not None
        assert "record SHA-256 collision detected" in file.error


def test_cleanup_of_already_published_stage_preserves_shared_evidence(postgres_engine: Engine) -> None:
    importer = _ingestor(postgres_engine, b'{"type":"assistant","message":{"usage":{"input_tokens":3}}}\n')
    assert importer.ingest().files_complete == 1
    with sessionmaker(postgres_engine)() as db, db.begin():
        version = db.scalar(select(FileVersionRow))
        record = db.scalar(select(FileVersionRecordRow))
        assert version is not None
        assert record is not None
        # A legacy writer can publish a version independently of a newer writer's staging.
        db.add(
            FileVersionImportRow(
                id=version.id,
                session_id=version.session_id,
                sha256=version.sha256,
                parser_version=version.parser_version,
                byte_size=version.byte_size,
                record_count=1,
                imported_record_count=1,
                parse_issue_count=0,
                parent_version_id=None,
                shared_prefix_record_count=0,
            )
        )
        db.flush()
        db.add(FileVersionImportRecordRow(import_id=version.id, ordinal=0, byte_offset=0, payload_id=record.payload_id))
    assert importer.ingest().files_complete == 1
    with sessionmaker(postgres_engine)() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionImportRow)) == 0
        assert db.scalar(select(func.count()).select_from(FileVersionImportRecordRow)) == 0
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 1
        assert db.scalar(select(func.count()).select_from(FileVersionRecordRow)) == 1
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 1
        assert db.scalar(select(func.count()).select_from(UsageObservationRow)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
