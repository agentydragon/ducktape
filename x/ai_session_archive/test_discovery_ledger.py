# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import FileVersionRow, ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]

MACHINE_ID = "discovery-ledger-test"
PATH = "/.claude/projects/project/session.jsonl"
START = datetime(2026, 10, 1, tzinfo=UTC)


def _snapshot(number: int, parent_id: str | None = None) -> ResticSnapshot:
    return ResticSnapshot(
        id=f"snapshot-{number}",
        captured_at=START + timedelta(minutes=number),
        parent_id=parent_id,
        tree_id=f"tree-{number}",
        paths=("/.claude",),
    )


class MemoryResticRepository(ResticRepository):
    def __init__(self, snapshots: list[ResticSnapshot]) -> None:
        super().__init__(binary="unused", environment={})
        self.snapshots = snapshots
        self.listed_files: list[str] = []
        self.diffed: list[tuple[str, str]] = []
        self.dumped: list[str] = []
        self.on_dump: Callable[[str], None] | None = None
        self.contents: dict[str, bytes] = {}

    def list_snapshots(self) -> list[ResticSnapshot]:
        return list(self.snapshots)

    def list_files(self, snapshot_id: str, path: str | None = None) -> list[ResticFile]:
        self.listed_files.append(snapshot_id)
        return [ResticFile(path=PATH, file_type="file", byte_size=1, modified_at=None)]

    def diff_snapshots(self, before_id: str, after_id: str) -> list[ResticChange]:
        self.diffed.append((before_id, after_id))
        return [ResticChange(path=PATH, modifier="M")]

    def dump_to(self, snapshot_id: str, path: str, destination: BinaryIO) -> None:
        self.dumped.append(snapshot_id)
        if self.on_dump is not None:
            self.on_dump(snapshot_id)
        destination.write(self.contents.get(snapshot_id, b'{"type":"user","message":{"content":"same"}}\n'))


def _ingestor(engine: Engine, repository: MemoryResticRepository) -> SnapshotIngestor:
    return SnapshotIngestor(
        sessionmaker(engine, expire_on_commit=False),
        ArchiveSource(
            machine_id=MACHINE_ID,
            display_name="Discovery ledger test host",
            restic=repository,
            roots=(HarnessRoot(Harness.CLAUDE, "/.claude"),),
        ),
    )


def test_discovery_is_committed_before_oldest_file_starts(postgres_engine: Engine) -> None:
    snapshots = [_snapshot(3), _snapshot(1), _snapshot(2)]
    repository = MemoryResticRepository(snapshots)
    factory = sessionmaker(postgres_engine)
    observed: list[tuple[list[tuple[str, str, str]], int]] = []

    def inspect_first_dump(snapshot_id: str) -> None:
        if snapshot_id != "snapshot-1":
            return
        with factory() as db:
            rows = list(db.scalars(select(ResticSnapshotRow).where(ResticSnapshotRow.machine_id == MACHINE_ID)))
            file_rows = list(db.scalars(select(SnapshotFileRow)))
        observed.append(
            (sorted((row.snapshot_id, row.inventory_status, row.processing_status) for row in rows), len(file_rows))
        )

    repository.on_dump = inspect_first_dump
    report = _ingestor(postgres_engine, repository).ingest()
    assert observed == [
        (
            [
                ("snapshot-1", "complete", "pending"),
                ("snapshot-2", "pending", "pending"),
                ("snapshot-3", "pending", "pending"),
            ],
            1,
        )
    ]
    assert repository.dumped == ["snapshot-1", "snapshot-2", "snapshot-3"]
    assert report.snapshots_seen == report.snapshots_complete == 3
    assert report.snapshots_incomplete == 0


def test_new_snapshots_are_discovered_on_next_run_without_resetting_existing_rows(postgres_engine: Engine) -> None:
    first = _snapshot(1)
    second = _snapshot(2, parent_id=first.id)
    repository = MemoryResticRepository([first])
    ingestor = _ingestor(postgres_engine, repository)
    assert ingestor.ingest().snapshots_complete == 1
    repository.snapshots.append(second)
    assert ingestor.ingest().snapshots_complete == 2
    assert repository.dumped == [first.id, second.id]
    assert repository.diffed == [(first.id, second.id)]
    with sessionmaker(postgres_engine)() as db:
        rows = list(db.scalars(select(ResticSnapshotRow).where(ResticSnapshotRow.machine_id == MACHINE_ID)))
    assert {row.snapshot_id for row in rows} == {first.id, second.id}
    assert all(row.processing_status == "complete" for row in rows)


def test_resumed_pending_ledger_retries_oldest_without_reimporting_complete_newer_snapshot(
    postgres_engine: Engine,
) -> None:
    first, second = _snapshot(1), _snapshot(2)
    repository = MemoryResticRepository([first, second])
    ingestor = _ingestor(postgres_engine, repository)

    def fail_oldest_once(snapshot_id: str) -> None:
        if snapshot_id == first.id:
            repository.on_dump = None
            raise OSError("temporary extraction failure")

    repository.on_dump = fail_oldest_once
    first_report = ingestor.ingest()
    assert first_report.snapshots_complete == 1
    assert first_report.snapshots_incomplete == 1
    with sessionmaker(postgres_engine)() as db:
        first_row = db.get(ResticSnapshotRow, (MACHINE_ID, first.id))
        second_row = db.get(ResticSnapshotRow, (MACHINE_ID, second.id))
        assert first_row is not None
        assert first_row.processing_status == "failed"
        assert second_row is not None
        assert second_row.processing_status == "complete"

    second_report = ingestor.ingest()
    assert second_report.snapshots_complete == 2
    assert second_report.snapshots_incomplete == 0
    assert repository.dumped == [first.id, second.id, first.id]
    with sessionmaker(postgres_engine)() as db:
        versions = list(db.scalars(select(FileVersionRow)))
    assert len(versions) == 1
    assert versions[0].first_seen_at == first.captured_at
    assert versions[0].first_snapshot_id == first.id
    assert versions[0].last_seen_at == second.captured_at


def test_pruned_parent_row_is_not_used_as_diff_base(postgres_engine: Engine) -> None:
    first = _snapshot(1)
    second = _snapshot(2, parent_id=first.id)
    repository = MemoryResticRepository([first])
    ingestor = _ingestor(postgres_engine, repository)
    ingestor.ingest()
    with sessionmaker(postgres_engine).begin() as db:
        old_row = db.get(ResticSnapshotRow, (MACHINE_ID, first.id))
        assert old_row is not None
        old_row.processing_status = "failed"
    repository.snapshots = [second]
    report = ingestor.ingest()
    assert report.snapshots_seen == report.snapshots_complete == 1
    assert report.snapshots_incomplete == 0
    assert repository.listed_files == [first.id, second.id]
    assert repository.diffed == []
    with sessionmaker(postgres_engine)() as db:
        row = db.get(ResticSnapshotRow, (MACHINE_ID, second.id))
        file_row = db.get(SnapshotFileRow, (MACHINE_ID, second.id, PATH))
        old_row = db.get(ResticSnapshotRow, (MACHINE_ID, first.id))
    assert old_row is not None
    assert old_row.processing_status == "failed"
    assert row is not None
    assert row.comparison_snapshot_id is None
    assert file_row is not None
    assert file_row.change_kind == "inventory"


if __name__ == "__main__":
    pytest_bazel.main()
