# gazelle:include_dep //util/testing:postgres_fixtures
import base64
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from util.testing.postgres import DatabaseNames, create_temporary_database
from x.ai_session_archive.database import (
    ResticSnapshotRow,
    Base,
    FileVersionRecordRow,
    FileVersionRow,
    RecordPayloadRow,
    SnapshotFileRow,
)
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor, _record_timestamp
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import get_session, list_events, list_sessions, list_versions
from x.ai_session_archive.restic import ResticChange, ResticRepository

pytest_plugins = ["util.testing.postgres_fixtures"]


@pytest.fixture
def postgres_engine(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[Engine]:
    with create_temporary_database(postgres_admin_url, database_names()) as database_url:
        engine = create_engine(database_url)
        try:
            Base.metadata.create_all(engine)
            yield engine
        finally:
            engine.dispose()


def _restic_binary() -> str:
    roots = [os.environ.get("RUNFILES_DIR"), os.environ.get("TEST_SRCDIR")]
    for root in roots:
        if root:
            for workspace in ("_main", "ducktape"):
                candidate = Path(root) / workspace / "third_party/restic/restic_bin"
                if candidate.is_file():
                    return str(candidate)
    raise AssertionError("pinned Restic binary is missing from test runfiles")


def _new_repository(tmp_path: Path) -> tuple[ResticRepository, dict[str, str]]:
    repository_path = tmp_path / "repository"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "RESTIC_REPOSITORY": str(repository_path),
        "RESTIC_PASSWORD": "test-only-passphrase",
        "RESTIC_CACHE_DIR": str(tmp_path / "restic-cache"),
    }
    Path(env["HOME"]).mkdir()
    Path(env["RESTIC_CACHE_DIR"]).mkdir()
    subprocess.run([_restic_binary(), "init"], env=env, check=True, capture_output=True)
    return ResticRepository(binary=_restic_binary(), environment=env), env


def _backup(env: dict[str, str], *source_paths: Path, force: bool = False) -> None:
    arguments = [_restic_binary(), "backup"]
    if force:
        arguments.append("--force")
    arguments.extend(("--host", "archive-test", *(str(path) for path in source_paths)))
    subprocess.run(arguments, env=env, check=True, capture_output=True)


def test_real_restic_import_retains_every_changed_and_deleted_version(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    codex_root = tmp_path / "home" / ".codex"
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
    ending_c = ending_b.replace(b'"ok"', b'"no"')
    main_path.write_bytes(repeated + repeated + ending_b)
    subagent_path.write_bytes(
        b'{"type":"assistant","sessionId":"agent-child","timestamp":"2026-01-01T00:00:02Z"}\n'
        b"\xff\x00\n"
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:03Z"}\n'
    )
    telemetry_path.write_bytes(b'{"event":"not a conversation"}\n')
    codex_path.write_bytes(b'{"type":"session_meta","payload":{"id":"archived-session","cwd":"/repo"}}\n["scalar",1]\n')

    _backup(env, claude_root, codex_root)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="archive-test",
        display_name="Archive test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)), HarnessRoot(Harness.CODEX, str(codex_root))),
    )
    ingestor = SnapshotIngestor(factory, source)
    report = ingestor.ingest()
    assert report.files_failed == 0
    assert report.snapshots_incomplete == 0

    with factory() as db:
        summaries = list_sessions(db, machine_id="archive-test", harness=Harness.CLAUDE, cursor=None, limit=100).data
        main = next(item for item in summaries if item.title == "hello")
        child = next(item for item in summaries if item.parent_session_id == main.id)
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

    original_stat = main_path.stat()
    main_path.write_bytes(repeated + repeated + ending_c)
    os.utime(main_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    _backup(env, claude_root, codex_root)
    snapshots = repository.list_snapshots()
    same_metadata_snapshot = next(snapshot for snapshot in snapshots if snapshot.id != initial_snapshot.id)
    assert same_metadata_snapshot.parent_id == initial_snapshot.id
    changes = repository.diff_snapshots(initial_snapshot.id, same_metadata_snapshot.id)
    assert any(change.path.endswith("/main-session.jsonl") and change.modifier == "M" for change in changes), [
        (change.path, change.modifier) for change in changes
    ]
    report = ingestor.ingest()
    assert report.files_failed == 0
    with factory() as db:
        main = next(
            item
            for item in list_sessions(
                db, machine_id="archive-test", harness=Harness.CLAUDE, cursor=None, limit=100
            ).data
            if item.title == "hello"
        )
        versions = list_versions(db, session_id=main.id)
        assert len(versions) == 2
        changed = next(version for version in versions if version.snapshot_id == same_metadata_snapshot.id)
        changed_events = list_events(db, session_id=main.id, version_id=changed.id, cursor=None, limit=20)
        assert changed_events is not None
        assert [event.ordinal for event in changed_events.data] == [0, 1, 2]
        assert changed_events.data[0].raw_base64 == changed_events.data[1].raw_base64
        changed_row = db.get(SnapshotFileRow, ("archive-test", same_metadata_snapshot.id, str(main_path)))
        assert changed_row is not None
        assert changed_row.change_kind == "M"
        changed_snapshot_row = db.get(ResticSnapshotRow, ("archive-test", same_metadata_snapshot.id))
        assert changed_snapshot_row is not None
        assert changed_snapshot_row.comparison_snapshot_id == initial_snapshot.id

    main_path.write_bytes(repeated)
    _backup(env, claude_root, codex_root)
    truncation_snapshot = repository.list_snapshots()[-1]
    ingestor.ingest()
    main_path.unlink()
    _backup(env, claude_root, codex_root)
    deletion_snapshot = repository.list_snapshots()[-1]
    ingestor.ingest()

    with factory() as db:
        main = next(
            item
            for item in list_sessions(
                db, machine_id="archive-test", harness=Harness.CLAUDE, cursor=None, limit=100
            ).data
            if item.title == "hello"
        )
        versions = list_versions(db, session_id=main.id)
        assert len(versions) == 3
        truncated = next(version for version in versions if version.snapshot_id == truncation_snapshot.id)
        events = list_events(db, session_id=main.id, version_id=truncated.id, cursor=None, limit=20)
        assert events is not None
        assert [event.ordinal for event in events.data] == [0]
        deletion = db.get(SnapshotFileRow, ("archive-test", deletion_snapshot.id, str(main_path)))
        assert deletion is not None
        assert deletion.status == "deleted"
        telemetry = list(
            db.scalars(
                select(SnapshotFileRow).where(
                    SnapshotFileRow.machine_id == "archive-test",
                    SnapshotFileRow.snapshot_id == initial_snapshot.id,
                    SnapshotFileRow.path == str(telemetry_path),
                )
            )
        )
        assert len(telemetry) == 1
        assert telemetry[0].status == "unclassified"
        snapshot_rows = list(
            db.scalars(select(ResticSnapshotRow).where(ResticSnapshotRow.machine_id == "archive-test"))
        )
        assert all(row.capture_status == "unknown" for row in snapshot_rows)


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
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 0

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_archive_record_insert ON file_version_records")
        connection.exec_driver_sql("DROP FUNCTION reject_archive_record_insert()")

    retried_report = ingestor.ingest()
    assert retried_report.files_failed == 0
    assert retried_report.snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 1
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 1


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


def test_restic_question_modifier_is_an_indexable_content_change() -> None:
    repository = ResticRepository(binary="unused", environment={})
    path = "/home/archive/.claude/projects/project/session.jsonl"
    with patch.object(
        repository,
        "_run",
        return_value=b'{"message_type":"change","path":"/home/archive/.claude/projects/project/session.jsonl","modifier":"?"}\n',
    ):
        changes = repository.diff_snapshots("before", "after")
    assert changes == [ResticChange(path=path, modifier="?")]

    source = ArchiveSource(
        machine_id="question-marker-test",
        display_name="Question marker test",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, "/home/archive/.claude"),),
    )
    ingestor = SnapshotIngestor(sessionmaker(), source)
    entries = ingestor._changed_entries(changes)
    assert entries[0][5] == "pending"
    assert entries[0][1] == "?"


@pytest.mark.parametrize("timestamp", ["2026-01-01T00:00:00", "not-a-timestamp"])
def test_timezone_naive_or_invalid_record_timestamps_are_unknown(timestamp: str) -> None:
    assert _record_timestamp({"timestamp": timestamp}) is None


if __name__ == "__main__":
    pytest_bazel.main()
