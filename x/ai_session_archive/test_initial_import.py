# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
from pathlib import Path

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import FileVersionRecordRow, ResticSnapshotRow, SnapshotFileRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import get_session, list_events, list_sessions, list_versions
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_initial_import_preserves_records_and_provenance(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
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
    codex_path.write_bytes(b'{"type":"session_meta","payload":{"id":"archived-session","cwd":"/repo"}}\n["scalar",1]\n')

    _backup(env, Path(".claude"), Path(".codex"), cwd=archive_home)
    initial_snapshot = repository.list_snapshots()[0]

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="archive-test",
        display_name="Archive test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, "/.claude"), HarnessRoot(Harness.CODEX, "/.codex")),
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


if __name__ == "__main__":
    pytest_bazel.main()
