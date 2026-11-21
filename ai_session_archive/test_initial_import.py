# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ai_session_archive.database import (
    FileVersionRecordRow,
    FileVersionRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
)
from ai_session_archive.models import Harness
from ai_session_archive.queries import get_session, list_sessions
from ai_session_archive.session_kind_projection import project_missing_session_kinds
from ai_session_archive.source_paths import HarnessRoot
from ai_session_archive.testing import backup, new_ingestor, new_repository
from ai_session_archive.version_queries import list_events, list_versions

pytest_plugins = ["testing.postgres_fixtures", "ai_session_archive.testing"]


def test_initial_import_preserves_records_and_provenance(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = new_repository(tmp_path)
    archive_home = tmp_path / "home"
    claude_root = archive_home / ".claude"
    codex_root = archive_home / ".codex"
    main_path = claude_root / "projects" / "example-project" / "main-session.jsonl"
    subagent_path = claude_root / "projects" / "example-project" / "main-session" / "subagents" / "agent-child.jsonl"
    telemetry_path = claude_root / "cache" / "telemetry.jsonl"
    codex_path = codex_root / "archived_sessions" / "archived-session.jsonl"
    for path in (main_path, subagent_path, telemetry_path, codex_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    repeated = (
        b'{"type":"user","sessionId":"main-session","cwd":"/repo","timestamp":"2026-01-01T00:00:00Z",'
        b'"message":{"content":"hello"}}\n'
    )
    ending_b = b'{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":"ok"}}\n'
    main_path.write_bytes(repeated + repeated + ending_b)
    subagent_path.write_bytes(
        b'{"type":"assistant","sessionId":"agent-child","timestamp":"2026-01-01T00:00:02Z"}\n'
        b"\xff\x00\n"
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:03Z"}\n'
    )
    telemetry_path.write_bytes(b'{"event":"not a conversation"}\n')
    codex_path.write_bytes(
        b'{"type":"session_meta","payload":{"id":"archived-session","cwd":"/repo","source":"cli"}}\n["scalar",1]\n'
    )

    backup(env, Path(".claude"), Path(".codex"), cwd=archive_home)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = new_ingestor(
        factory,
        repository,
        HarnessRoot(Harness.CLAUDE, "/.claude"),
        HarnessRoot(Harness.CODEX, "/.codex"),
        machine_id="archive-test",
    )
    report = ingestor.ingest()
    assert report.files_failed == 0
    assert report.snapshots_incomplete == 0

    with factory() as db, db.begin():
        codex_session = db.scalar(
            select(SessionRow).where(SessionRow.machine_id == "archive-test", SessionRow.harness == Harness.CODEX.value)
        )
        assert codex_session is not None
        checkpoint = db.scalar(select(FileVersionRow).where(FileVersionRow.session_id == codex_session.id))
        assert checkpoint is not None
        inherited_record = db.execute(
            select(FileVersionRecordRow.payload_id, FileVersionRecordRow.byte_offset).where(
                FileVersionRecordRow.file_version_id == checkpoint.id, FileVersionRecordRow.ordinal == 1
            )
        ).one()
        delta = FileVersionRow(
            id=uuid4(),
            session_id=checkpoint.session_id,
            sha256="f" * 64,
            byte_size=checkpoint.byte_size,
            source_path=checkpoint.source_path,
            first_seen_at=checkpoint.last_seen_at + timedelta(seconds=1),
            last_seen_at=checkpoint.last_seen_at + timedelta(seconds=1),
            first_snapshot_id=checkpoint.first_snapshot_id,
            complete=True,
            parse_issue_count=0,
            record_count=checkpoint.record_count,
            parent_version_id=checkpoint.id,
            shared_prefix_record_count=1,
            is_checkpoint=False,
            delta_depth=checkpoint.delta_depth + 1,
            parser_version=checkpoint.parser_version,
        )
        db.add(delta)
        db.flush()
        db.add(
            FileVersionRecordRow(
                file_version_id=delta.id,
                ordinal=1,
                byte_offset=inherited_record.byte_offset,
                payload_id=inherited_record.payload_id,
            )
        )
        for row in db.scalars(select(SessionRow).where(SessionRow.machine_id == "archive-test")):
            row.session_kind = None
    project_missing_session_kinds(factory, "archive-test")

    with factory() as db:
        summaries = list_sessions(db, machine_id="archive-test", harness=Harness.CLAUDE, cursor=None, limit=100).data
        main = next(item for item in summaries if item.title == "hello")
        child = next(item for item in summaries if item.parent_session_id == main.id)
        assert main.session_kind.value == "unknown"
        assert child.session_kind.value == "subagent"
        assert child.parent_link_kind == "claude_path_inferred"
        assert not child.identity_inferred
        children = list_sessions(
            db, machine_id="archive-test", harness=Harness.CLAUDE, parent_session_id=main.id, cursor=None, limit=100
        )
        assert [item.id for item in children.data] == [child.id]
        child_versions = list_versions(db, session_id=child.id)
        child_events = list_events(db, session_id=child.id, version_id=child_versions[0].id, cursor=None, limit=20)
        assert child_events is not None
        assert child_events.data[1].parse_error is not None
        assert child_events.data[1].raw_base64 == base64.b64encode(b"\xff\x00\n").decode("ascii")
        child_last_record = child_events.data[2].parsed
        assert isinstance(child_last_record, dict)
        assert child_last_record["type"] == "assistant"
        codex_sessions = list_sessions(db, machine_id="archive-test", harness=Harness.CODEX, cursor=None, limit=100)
        assert len(codex_sessions.data) == 1
        assert codex_sessions.data[0].session_kind.value == "main"
        codex_versions = list_versions(db, session_id=codex_sessions.data[0].id)
        codex_events = list_events(
            db, session_id=codex_sessions.data[0].id, version_id=codex_versions[0].id, cursor=None, limit=20
        )
        assert codex_events is not None
        assert codex_events.data[1].parsed == ["scalar", 1]
        codex_sessions = list_sessions(db, machine_id="archive-test", harness=Harness.CODEX, cursor=None, limit=100)
        assert len(codex_sessions.data) == 1
        assert codex_sessions.data[0].identity_inferred is False

        versions = list_versions(db, session_id=main.id)
        first_version = next(version for version in versions if version.snapshot_id == initial_snapshot.id)
        first_events = list_events(db, session_id=main.id, version_id=first_version.id, cursor=None, limit=20)
        assert first_events is not None
        assert [event.ordinal for event in first_events.data] == [0, 1, 2]
        first_page = list_events(db, session_id=main.id, version_id=first_version.id, cursor=None, limit=1)
        assert first_page is not None
        assert [event.ordinal for event in first_page.data] == [0]
        assert first_page.next_cursor == 0
        second_page = list_events(
            db, session_id=main.id, version_id=first_version.id, cursor=first_page.next_cursor, limit=1
        )
        assert second_page is not None
        assert [event.ordinal for event in second_page.data] == [1]
        assert second_page.next_cursor == 1
        assert first_events.data[0].timestamp is not None
        assert first_events.data[0].timestamp.utcoffset() is not None
        assert first_events.data[0].raw_base64 == first_events.data[1].raw_base64
        assert (
            len(
                list(
                    db.scalars(
                        select(FileVersionRecordRow).where(FileVersionRecordRow.file_version_id == first_version.id)
                    )
                )
            )
            == 3
        )
        assert get_session(db, session_id=main.id) is not None

    with factory() as db:
        telemetry = list(
            db.scalars(
                select(SnapshotFileRow).where(
                    SnapshotFileRow.machine_id == "archive-test",
                    SnapshotFileRow.snapshot_id == initial_snapshot.id,
                    SnapshotFileRow.path == "/.claude/cache/telemetry.jsonl",
                )
            )
        )
        assert len(telemetry) == 1
        assert telemetry[0].status == "unclassified"
        snapshot_rows = list(
            db.scalars(select(ResticSnapshotRow).where(ResticSnapshotRow.machine_id == "archive-test"))
        )
        assert all(row.capture_status == "unknown" for row in snapshot_rows)


def test_changed_file_version_is_kept_with_same_metadata_diff(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    main_path = claude_root / "projects" / "example-project" / "main-session.jsonl"
    main_path.parent.mkdir(parents=True)
    repeated = b'{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}\n'
    ending_b = b'{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":"ok"}}\n'
    ending_c = ending_b.replace(b'"ok"', b'"no"')
    main_path.write_bytes(repeated + repeated + ending_b)
    backup(env, claude_root)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = new_ingestor(
        factory, repository, HarnessRoot(Harness.CLAUDE, str(claude_root)), machine_id="archive-modified-test"
    )
    report = ingestor.ingest()
    assert report.files_failed == 0

    original_stat = main_path.stat()
    main_path.write_bytes(repeated + repeated + ending_c)
    os.utime(main_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    backup(env, claude_root)
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


def test_parentless_restic_snapshot_uses_full_inventory(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"content":"original"}}\n')
    backup(env, claude_root)
    first_snapshot = repository.list_snapshots()[0]
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = new_ingestor(
        factory, repository, HarnessRoot(Harness.CLAUDE, str(claude_root)), machine_id="parentless-test"
    )
    ingestor.ingest()

    transcript.write_bytes(b'{"type":"user","message":{"content":"forced snapshot"}}\n')
    backup(env, claude_root, force=True)
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
