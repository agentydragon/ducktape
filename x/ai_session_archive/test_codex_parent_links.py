# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticChange, ResticFile, ResticRepository, ResticSnapshot

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_codex_parent_links_resolve_late_and_mark_conflicting_identity_ambiguous(postgres_engine: Engine) -> None:
    codex_root = "/home/archive/.codex"
    child_path = f"{codex_root}/sessions/2026/10/child.jsonl"
    parent_path = f"{codex_root}/sessions/2026/10/parent.jsonl"
    duplicate_parent_path = f"{codex_root}/sessions/2026/10/parent-copy.jsonl"
    base_time = datetime(2026, 10, 1, tzinfo=UTC)
    snapshots = [
        ResticSnapshot("snapshot-child", base_time, None, "tree-child", (codex_root,)),
        ResticSnapshot(
            "snapshot-parent", base_time + timedelta(minutes=1), "snapshot-child", "tree-parent", (codex_root,)
        ),
        ResticSnapshot(
            "snapshot-conflicting-parent",
            base_time + timedelta(minutes=2),
            "snapshot-parent",
            "tree-conflicting-parent",
            (codex_root,),
        ),
        ResticSnapshot(
            "snapshot-duplicate-parent",
            base_time + timedelta(minutes=3),
            "snapshot-conflicting-parent",
            "tree-duplicate-parent",
            (codex_root,),
        ),
        ResticSnapshot(
            "snapshot-conflicting-child-parent",
            base_time + timedelta(minutes=4),
            "snapshot-duplicate-parent",
            "tree-conflicting-child-parent",
            (codex_root,),
        ),
    ]
    contents = {
        (
            snapshots[0].id,
            child_path,
        ): b'{"type":"session_meta","payload":{"id":"child","parent_thread_id":"parent"}}\n',
        (snapshots[1].id, parent_path): b'{"type":"session_meta","payload":{"id":"parent"}}\n',
        (snapshots[2].id, parent_path): b'{"type":"session_meta","payload":{"id":"replaced-parent"}}\n',
        (snapshots[3].id, duplicate_parent_path): b'{"type":"session_meta","payload":{"id":"parent"}}\n',
        (
            snapshots[4].id,
            child_path,
        ): b'{"type":"session_meta","payload":{"id":"child","parent_thread_id":"other-parent"}}\n',
    }
    files_by_snapshot = {
        snapshot.id: [
            ResticFile(path, "file", len(content), snapshot.captured_at)
            for (snapshot_id, path), content in contents.items()
            if snapshot_id == snapshot.id
        ]
        for snapshot in snapshots
    }
    changes_by_pair = {
        (snapshots[0].id, snapshots[1].id): [ResticChange(parent_path, "+")],
        (snapshots[1].id, snapshots[2].id): [ResticChange(parent_path, "M")],
        (snapshots[2].id, snapshots[3].id): [ResticChange(duplicate_parent_path, "+")],
        (snapshots[3].id, snapshots[4].id): [ResticChange(child_path, "M")],
    }
    repository = ResticRepository(binary="unused", environment={})
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory,
        ArchiveSource(
            machine_id="codex-parent-test",
            display_name="Codex parent test host",
            restic=repository,
            roots=(HarnessRoot(Harness.CODEX, codex_root),),
        ),
    )
    available = snapshots[:1]
    with (
        patch.object(repository, "list_snapshots", side_effect=lambda: available),
        patch.object(
            repository, "list_files", side_effect=lambda snapshot_id, path=None: files_by_snapshot[snapshot_id]
        ),
        patch.object(repository, "diff_snapshots", side_effect=lambda before, after: changes_by_pair[(before, after)]),
        patch.object(
            repository,
            "dump_to",
            side_effect=lambda snapshot_id, path, destination: destination.write(contents[(snapshot_id, path)]),
        ),
    ):
        first_report = ingestor.ingest()
        assert first_report.files_failed == 0
        with factory() as db:
            child = db.scalar(select(SessionRow).where(SessionRow.machine_id == "codex-parent-test"))
            assert child is not None
            assert child.native_session_id == "child"
            assert child.parent_native_session_id == "parent"
            assert child.parent_session_id is None
            assert child.parent_link_kind == "codex_parent_thread_id_unresolved"

        available = snapshots[:2]
        second_report = ingestor.ingest()
        assert second_report.files_failed == 0
        with factory() as db:
            child = db.scalar(select(SessionRow).where(SessionRow.native_session_id == "child"))
            parent = db.scalar(select(SessionRow).where(SessionRow.native_session_id == "parent"))
            assert child is not None
            assert parent is not None
            assert child.parent_session_id == parent.id
            assert child.parent_link_kind == "codex_parent_thread_id"

        available = snapshots[:3]
        third_report = ingestor.ingest()
        assert third_report.files_failed == 0
        with factory() as db:
            child = db.scalar(select(SessionRow).where(SessionRow.native_session_id == "child"))
            conflicted_parent = db.scalar(select(SessionRow).where(SessionRow.source_path == parent_path))
            assert child is not None
            assert conflicted_parent is not None
            assert conflicted_parent.native_session_id is None
            assert conflicted_parent.native_session_id_conflict
            assert conflicted_parent.native_session_id_candidates == ["parent", "replaced-parent"]
            assert child.parent_session_id is None
            assert child.parent_link_kind == "codex_parent_thread_id_inconsistent"

        available = snapshots[:4]
        fourth_report = ingestor.ingest()
        assert fourth_report.files_failed == 0
        with factory() as db:
            parents = list(db.scalars(select(SessionRow).where(SessionRow.native_session_id == "parent")))
            child = db.scalar(select(SessionRow).where(SessionRow.native_session_id == "child"))
            assert len(parents) == 1
            assert parents[0].source_path == duplicate_parent_path
            assert child is not None
            assert child.parent_native_session_id == "parent"
            assert child.parent_session_id is None
            assert child.parent_link_kind == "codex_parent_thread_id_inconsistent"

        available = snapshots
        fifth_report = ingestor.ingest()
        assert fifth_report.files_failed == 0
        with factory() as db:
            child = db.scalar(select(SessionRow).where(SessionRow.native_session_id == "child"))
            assert child is not None
            assert child.parent_native_session_id is None
            assert child.parent_native_session_id_conflict
            assert child.parent_native_session_id_candidates == ["other-parent", "parent"]
            assert child.parent_session_id is None
            assert child.parent_link_kind == "codex_parent_thread_id_inconsistent"


if __name__ == "__main__":
    pytest_bazel.main()
