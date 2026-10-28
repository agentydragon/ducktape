# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""Scheduling and provenance checks for incremental Restic ingestion."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import FileVersionRow, ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]

ROOT = "/.codex"
TIME = datetime(2026, 10, 28, tzinfo=UTC)
CONTENT = b'{"type":"event_msg","payload":{"type":"user_message","message":"hello"}}\n'


def _snapshot(digit: str, minutes: int, parent: str | None = None) -> ResticSnapshot:
    return ResticSnapshot(digit * 64, TIME + timedelta(minutes=minutes), parent, digit * 64, (ROOT,))


def _source(repository: ResticRepository) -> ArchiveSource:
    return ArchiveSource("fresh-test", "Fresh test", repository, (HarnessRoot(Harness.CODEX, ROOT),))


def _path(name: str) -> str:
    return f"{ROOT}/sessions/2026/10/28/{name}.jsonl"


def test_new_snapshots_preempt_backfill_while_oldest_keeps_progressing(postgres_engine: Engine) -> None:
    repository = ResticRepository(binary="unused", environment={})
    old, middle, recent, arriving = (_snapshot("a", 0), _snapshot("b", 1), _snapshot("c", 2), _snapshot("d", 3))
    inventory = {
        old.id: [ResticFile(_path("oldest"), "file", len(CONTENT), TIME)],
        middle.id: [ResticFile(_path("middle"), "file", len(CONTENT), TIME)],
        recent.id: [
            ResticFile(_path(f"recent-{index:02}"), "file", len(CONTENT), TIME + timedelta(minutes=index))
            for index in range(33)
        ],
        arriving.id: [ResticFile(_path("arriving"), "file", len(CONTENT), TIME + timedelta(minutes=34))],
    }
    listed = 0
    dumped: list[tuple[str, str]] = []

    def list_snapshots() -> list[ResticSnapshot]:
        nonlocal listed
        listed += 1
        return [old, middle, recent] if listed == 1 else [old, middle, recent, arriving]

    def dump(snapshot_id: str, path: str, destination: object) -> None:
        dumped.append((snapshot_id, path))
        destination.write(CONTENT)  # type: ignore[attr-defined]

    with (
        patch.object(repository, "list_snapshots", side_effect=list_snapshots),
        patch.object(repository, "list_files", side_effect=lambda snapshot_id: inventory[snapshot_id]),
        patch.object(repository, "diff_snapshots") as diff,
        patch.object(repository, "dump_to", side_effect=dump),
    ):
        report = SnapshotIngestor(sessionmaker(postgres_engine, expire_on_commit=False), _source(repository)).ingest()

    assert report.snapshots_complete == 4
    assert report.snapshots_incomplete == 0
    assert listed >= 3
    diff.assert_not_called()
    assert dumped[0] == (recent.id, _path("recent-32"))
    assert dumped.index((old.id, _path("oldest"))) < dumped.index((arriving.id, _path("arriving")))
    assert dumped.index((arriving.id, _path("arriving"))) < dumped.index((recent.id, _path("recent-00")))
    assert dumped.index((middle.id, _path("middle"))) < dumped.index((recent.id, _path("recent-00")))


def test_partial_freshness_stage_bounds_work_as_snapshots_keep_arriving(postgres_engine: Engine) -> None:
    repository = ResticRepository(binary="unused", environment={})
    old, recent, arriving = _snapshot("a", 0), _snapshot("b", 10, "a" * 64), _snapshot("c", 20, "b" * 64)
    stable = [
        ResticFile(_path(f"stable-{index:03}"), "file", len(CONTENT), TIME + timedelta(minutes=index))
        for index in range(129)
    ]
    first_change = ResticFile(_path("fresh-one"), "file", len(CONTENT), TIME + timedelta(days=1))
    second_change = ResticFile(_path("fresh-two"), "file", len(CONTENT), TIME + timedelta(days=2))
    inventories = {
        old.id: stable,
        recent.id: [*stable, first_change],
        arriving.id: [*stable, first_change, second_change],
    }
    changes = {
        (old.id, recent.id): [ResticChange(first_change.path, "+")],
        (recent.id, arriving.id): [ResticChange(second_change.path, "+")],
    }
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    listed = 0
    dumped: list[tuple[str, str]] = []

    def list_snapshots() -> list[ResticSnapshot]:
        nonlocal listed
        listed += 1
        if listed == 2:
            with factory() as db:
                recent_rows = db.scalar(
                    select(func.count()).select_from(SnapshotFileRow).where(SnapshotFileRow.snapshot_id == recent.id)
                )
                assert recent_rows == 64  # one diff plus bounded recent seed, not all 130 files
                assert db.get(SnapshotFileRow, ("fresh-test", recent.id, stable[-1].path)) is not None
                oldest = db.get(ResticSnapshotRow, ("fresh-test", old.id))
                assert oldest is not None
                assert oldest.processing_status == "pending"
        if listed == 3:
            with factory() as db:
                arriving_rows = db.scalar(
                    select(func.count()).select_from(SnapshotFileRow).where(SnapshotFileRow.snapshot_id == arriving.id)
                )
                assert arriving_rows == 1  # no repeated seed after a recent version was persisted
        return [old, recent] if listed == 1 else [old, recent, arriving]

    def dump(snapshot_id: str, path: str, destination: object) -> None:
        dumped.append((snapshot_id, path))
        destination.write(CONTENT)  # type: ignore[attr-defined]

    with (
        patch.object(repository, "list_snapshots", side_effect=list_snapshots),
        patch.object(repository, "list_files", side_effect=lambda snapshot_id: inventories[snapshot_id]),
        patch.object(repository, "diff_snapshots", side_effect=lambda before, after: changes[(before, after)]),
        patch.object(repository, "dump_to", side_effect=dump),
    ):
        report = SnapshotIngestor(factory, _source(repository)).ingest()

    assert report.snapshots_complete == 3
    assert dumped.index((arriving.id, second_change.path)) < dumped.index((old.id, stable[-1].path))


def test_pruned_parent_uses_full_inventory_even_if_ledger_keeps_it(postgres_engine: Engine) -> None:
    repository = ResticRepository(binary="unused", environment={})
    old, child = _snapshot("a", 0), _snapshot("b", 1, "a" * 64)
    available = [old]
    path = _path("same-session")
    with (
        patch.object(repository, "list_snapshots", side_effect=lambda: available),
        patch.object(
            repository, "list_files", side_effect=lambda snapshot_id: [ResticFile(path, "file", len(CONTENT), TIME)]
        ),
        patch.object(repository, "diff_snapshots") as diff,
        patch.object(
            repository, "dump_to", side_effect=lambda snapshot_id, path, destination: destination.write(CONTENT)
        ),
    ):
        ingestor = SnapshotIngestor(sessionmaker(postgres_engine, expire_on_commit=False), _source(repository))
        assert ingestor.ingest().snapshots_complete == 1
        available = [child]
        assert ingestor.ingest().snapshots_complete == 2

    diff.assert_not_called()
    with sessionmaker(postgres_engine)() as db:
        row = db.get(ResticSnapshotRow, ("fresh-test", child.id))
        file = db.get(SnapshotFileRow, ("fresh-test", child.id, path))
        assert row is not None
        assert row.comparison_snapshot_id is None
        assert file is not None
        assert file.change_kind == "inventory"


def test_older_duplicate_content_corrects_first_seen_provenance(postgres_engine: Engine) -> None:
    repository = ResticRepository(binary="unused", environment={})
    old, recent = _snapshot("a", 0), _snapshot("b", 10)
    path = _path("same-session")
    with (
        patch.object(repository, "list_snapshots", return_value=[old, recent]),
        patch.object(
            repository, "list_files", side_effect=lambda snapshot_id: [ResticFile(path, "file", len(CONTENT), TIME)]
        ),
        patch.object(repository, "diff_snapshots") as diff,
        patch.object(
            repository, "dump_to", side_effect=lambda snapshot_id, path, destination: destination.write(CONTENT)
        ),
    ):
        report = SnapshotIngestor(sessionmaker(postgres_engine, expire_on_commit=False), _source(repository)).ingest()

    assert report.snapshots_complete == 2
    diff.assert_not_called()
    with sessionmaker(postgres_engine)() as db:
        old_file = db.get(SnapshotFileRow, ("fresh-test", old.id, path))
        recent_file = db.get(SnapshotFileRow, ("fresh-test", recent.id, path))
        assert old_file is not None
        assert recent_file is not None
        assert old_file.file_version_id == recent_file.file_version_id
        version = db.get(FileVersionRow, old_file.file_version_id)
        assert version is not None
        assert version.first_seen_at == old.captured_at
        assert version.first_snapshot_id == old.id
        assert version.last_seen_at == recent.captured_at


if __name__ == "__main__":
    pytest_bazel.main()
