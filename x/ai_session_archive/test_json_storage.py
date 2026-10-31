# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest_bazel
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.backfill_jsonb_payloads import backfill_jsonb_payloads
from x.ai_session_archive.database import (
    FileVersionRecordRow,
    FileVersionRow,
    MachineRow,
    RecordPayloadRow,
    ResticSnapshotRow,
    SessionRow,
    SnapshotFileRow,
)
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor, _parse_record_content
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import list_events, list_sessions, list_versions
from x.ai_session_archive.testing import _backup, _new_repository
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_json_null_and_syntax_errors_keep_distinct_parse_state() -> None:
    json_null = _parse_record_content(b"null")
    invalid = _parse_record_content(b'{"type": }')

    assert json_null.parsed is None
    assert json_null.parsed_from_raw is False
    assert json_null.parse_error is None
    assert invalid.parsed is None
    assert invalid.parsed_from_raw is False
    assert invalid.parse_error is not None


def test_ingest_and_http_serve_valid_jsonb_fallbacks(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, environment = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    lines = [
        b'{"type":"session_meta","payload":{"id":"bad\\u0000id","parent_thread_id":"bad\\ud800id"},"cwd":"bad\\u0000cwd"}\n',
        b'{"type":"user","title":"bad\\ud800title","cwd":"bad\\ud800cwd","message":{"content":"good title"}}\n',
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:00Z","nested":{"nul":"\\u0000","surrogate":"\\ud800"}}\n',
        b'{"type":"number","value":1e9999}\n',
        b"null\n",
        b'{"type": }\n',
    ]
    transcript.write_bytes(b"".join(lines))
    _backup(environment, claude_root)

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="json-storage-test",
        display_name="JSON storage test",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    report = SnapshotIngestor(factory, source).ingest()
    with factory() as db:
        failed_files = list(db.scalars(select(SnapshotFileRow).where(SnapshotFileRow.status == "failed")))
    assert report.files_failed == 0, [file.error for file in failed_files]
    assert report.files_partial == 1

    with factory() as db:
        session = list_sessions(db, machine_id="json-storage-test", harness=Harness.CLAUDE, cursor=None, limit=10).data[
            0
        ]
        version = list_versions(db, session_id=session.id)[0]
        events = list_events(db, session_id=session.id, version_id=version.id, cursor=None, limit=10)
        session_row = db.get(SessionRow, session.id)
        payloads = list(db.scalars(select(RecordPayloadRow).order_by(RecordPayloadRow.id)))
        assert events is not None
        assert session.title == "good title"
        assert session_row is not None
        assert session_row.cwd is None
        assert session_row.parent_native_session_id is None
        assert version.parse_issue_count == 1
        assert version.complete is False
        assert len(events.data) == 6
        assert isinstance(events.data[2].parsed, dict)
        assert events.data[2].parsed["nested"] == {"nul": "\x00", "surrogate": "\ud800"}
        assert isinstance(events.data[3].parsed, dict)
        assert events.data[3].parsed["value"] == float("inf")
        assert events.data[4].parsed is None
        assert events.data[4].parse_error is None
        assert events.data[5].parsed is None
        assert events.data[5].parse_error is not None
        fallback_payloads = [payload for payload in payloads if payload.raw_bytes in lines[:4]]
        assert len(fallback_payloads) == 4
        assert all(payload.parsed_from_raw and payload.parse_error is None for payload in fallback_payloads)
        assert all(payload.kind is not None for payload in fallback_payloads)

    fallback_id = next(payload.id for payload in fallback_payloads if b"nested" in payload.raw_bytes)
    json_null_id = next(payload.id for payload in payloads if payload.raw_bytes == b"null\n")
    with postgres_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT parsed IS NULL FROM record_payloads WHERE id = %s", (fallback_id,)
        ).scalar_one()
        assert not connection.exec_driver_sql(
            "SELECT parsed IS NULL FROM record_payloads WHERE id = %s", (json_null_id,)
        ).scalar_one()

    settings = WebSettings(
        database_url="postgresql+psycopg://archive_user:archive_password@db.invalid/archive",
        public_base_url="http://testserver",
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-client-secret",
        oidc_session_secret="fake-session-secret",
        oidc_allowed_subject="fake-owner",
    )
    client = TestClient(create_app(engine=postgres_engine, settings=settings))
    signed_in = {
        "user": {
            "issuer": settings.oidc_issuer,
            "subject": settings.oidc_allowed_subject,
            "username": "fake-user",
            "expires_at": time.time() + 600,
        }
    }
    cookie_payload = base64.b64encode(json.dumps(signed_in).encode())
    client.cookies.set("ai-session-archive", TimestampSigner("fake-session-secret").sign(cookie_payload).decode())
    response = client.get(f"/api/sessions/{session.id}/versions/{version.id}/events")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert b"\\u0000" in response.content
    assert b"\\ud800" in response.content
    assert b"1e9999" in response.content
    assert b"Infinity" not in response.content
    assert b"\x00" not in response.content
    http_events = response.json()["data"]
    assert http_events[2]["parsed"]["nested"] == {"nul": "\x00", "surrogate": "\ud800"}
    assert http_events[3]["parsed"]["value"] == float("inf")
    assert http_events[4]["parsed"] is None
    assert http_events[4]["parse_error"] is None
    assert http_events[5]["parsed"] is None
    assert http_events[5]["parse_error"] is not None


def test_jsonb_backfill_recomputes_shared_prefix_and_is_idempotent(postgres_engine: Engine) -> None:
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    session_id, first_version_id, second_version_id, third_version_id = uuid4(), uuid4(), uuid4(), uuid4()
    payload_ids = [uuid4() for _ in range(4)]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raw_lines = [
        b'{"type":"user"}\n',
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:00Z","nested":{"value":"\\u0000"}}\n',
        b'{"type": }\n',
        b'{"type":"assistant","message":"new"}\n',
    ]
    lineage_content = (raw_lines[0], raw_lines[1], raw_lines[1], raw_lines[2])
    errors = [None, "ValueError: JSONB cannot store NUL at $.nested.value", "JSONDecodeError: invalid JSON", None]
    with factory() as db, db.begin():
        db.add(MachineRow(id="json-backfill-test", display_name="JSON backfill test"))
        db.add(
            SessionRow(
                id=session_id,
                machine_id="json-backfill-test",
                harness=Harness.CLAUDE.value,
                source_path="/archive/session.jsonl",
                source_relative_path="session.jsonl",
            )
        )
        db.add_all(
            [
                ResticSnapshotRow(
                    machine_id="json-backfill-test",
                    snapshot_id=snapshot_id,
                    captured_at=now,
                    paths=["/archive"],
                    inventory_status="complete",
                    processing_status="complete",
                    codex_metadata_status="complete",
                )
                for snapshot_id in ("first-snapshot", "second-snapshot", "third-snapshot")
            ]
        )
        payloads = [
            RecordPayloadRow(
                id=payload_id,
                sha256=hashlib.sha256(raw_line).hexdigest(),
                parser_version="jsonl-v1",
                raw_bytes=raw_line,
                parse_error=parse_error,
                timestamp=None,
                kind=None,
            )
            for payload_id, raw_line, parse_error in zip(payload_ids, raw_lines, errors, strict=True)
        ]
        db.add_all(payloads)
        db.flush()
        first_version = FileVersionRow(
            id=first_version_id,
            session_id=session_id,
            sha256="a" * 64,
            byte_size=sum(map(len, lineage_content)),
            source_path="/archive/session.jsonl",
            first_seen_at=now,
            last_seen_at=now,
            first_snapshot_id="first-snapshot",
            complete=False,
            parse_issue_count=3,
            record_count=4,
            parent_version_id=None,
            shared_prefix_record_count=0,
            is_checkpoint=True,
            delta_depth=0,
            parser_version="jsonl-v1",
        )
        db.add(first_version)
        db.flush()
        second_version = FileVersionRow(
            id=second_version_id,
            session_id=session_id,
            sha256="b" * 64,
            byte_size=sum(map(len, (raw_lines[0], raw_lines[1], raw_lines[1], raw_lines[3]))),
            source_path="/archive/session.jsonl",
            first_seen_at=now,
            last_seen_at=now,
            first_snapshot_id="second-snapshot",
            complete=False,
            parse_issue_count=2,
            record_count=4,
            parent_version_id=first_version_id,
            shared_prefix_record_count=3,
            is_checkpoint=False,
            delta_depth=1,
            parser_version="jsonl-v1",
        )
        db.add(second_version)
        db.flush()
        third_version = FileVersionRow(
            id=third_version_id,
            session_id=session_id,
            sha256="c" * 64,
            byte_size=sum(map(len, (raw_lines[0], raw_lines[3], raw_lines[2]))),
            source_path="/archive/session.jsonl",
            first_seen_at=now,
            last_seen_at=now,
            first_snapshot_id="third-snapshot",
            complete=False,
            parse_issue_count=1,
            record_count=3,
            parent_version_id=first_version_id,
            shared_prefix_record_count=1,
            is_checkpoint=False,
            delta_depth=1,
            parser_version="jsonl-v1",
        )
        db.add(third_version)
        db.flush()
        db.add_all(
            [
                FileVersionRecordRow(
                    file_version_id=first_version_id, ordinal=0, byte_offset=0, payload_id=payload_ids[0]
                ),
                FileVersionRecordRow(
                    file_version_id=first_version_id,
                    ordinal=1,
                    byte_offset=len(raw_lines[0]),
                    payload_id=payload_ids[1],
                ),
                FileVersionRecordRow(
                    file_version_id=first_version_id,
                    ordinal=2,
                    byte_offset=len(raw_lines[0]) + len(raw_lines[1]),
                    payload_id=payload_ids[1],
                ),
                FileVersionRecordRow(
                    file_version_id=first_version_id,
                    ordinal=3,
                    byte_offset=len(raw_lines[0]) + 2 * len(raw_lines[1]),
                    payload_id=payload_ids[2],
                ),
                FileVersionRecordRow(
                    file_version_id=second_version_id,
                    ordinal=3,
                    byte_offset=len(raw_lines[0]) + 2 * len(raw_lines[1]),
                    payload_id=payload_ids[3],
                ),
                FileVersionRecordRow(
                    file_version_id=third_version_id,
                    ordinal=1,
                    byte_offset=len(raw_lines[0]),
                    payload_id=payload_ids[3],
                ),
                FileVersionRecordRow(
                    file_version_id=third_version_id,
                    ordinal=2,
                    byte_offset=len(raw_lines[0]) + len(raw_lines[3]),
                    payload_id=payload_ids[2],
                ),
            ]
        )
        db.add_all(
            [
                SnapshotFileRow(
                    machine_id="json-backfill-test",
                    snapshot_id=snapshot_id,
                    path="/archive/session.jsonl",
                    session_id=session_id,
                    change_kind="added",
                    file_type="file",
                    byte_size=version.byte_size,
                    status="partial",
                    file_version_id=version.id,
                )
                for snapshot_id, version in (
                    ("first-snapshot", first_version),
                    ("second-snapshot", second_version),
                    ("third-snapshot", third_version),
                )
            ]
        )

    report = backfill_jsonb_payloads(factory, limit=1)
    assert report.inspected_payload_count == 1
    assert report.repaired_payload_count == 1
    assert report.more_candidates is False
    assert report.recomputed_version_count == 2
    assert report.updated_snapshot_file_count == 1
    assert report.metadata_review_session_count == 1
    assert report.metadata_review_session_ids == [str(session_id)]
    with factory() as db:
        repaired = db.get(RecordPayloadRow, payload_ids[1])
        genuine_error = db.get(RecordPayloadRow, payload_ids[2])
        stored_first_version = db.get(FileVersionRow, first_version_id)
        stored_second_version = db.get(FileVersionRow, second_version_id)
        stored_third_version = db.get(FileVersionRow, third_version_id)
        first_file = db.get(SnapshotFileRow, ("json-backfill-test", "first-snapshot", "/archive/session.jsonl"))
        second_file = db.get(SnapshotFileRow, ("json-backfill-test", "second-snapshot", "/archive/session.jsonl"))
        third_file = db.get(SnapshotFileRow, ("json-backfill-test", "third-snapshot", "/archive/session.jsonl"))
        assert repaired is not None
        assert genuine_error is not None
        assert stored_first_version is not None
        assert stored_second_version is not None
        assert stored_third_version is not None
        assert first_file is not None
        assert second_file is not None
        assert third_file is not None
        assert repaired.raw_bytes == raw_lines[1]
        assert repaired.parser_version == "jsonl-v1"
        assert repaired.parsed_from_raw is True
        assert repaired.parsed is None
        assert repaired.parse_error is None
        assert repaired.kind == "assistant"
        assert repaired.timestamp == datetime(2026, 1, 1, tzinfo=UTC)
        assert genuine_error.parsed_from_raw is False
        assert genuine_error.parse_error is not None
        assert stored_first_version.parse_issue_count == 1
        assert stored_first_version.complete is False
        assert stored_second_version.parse_issue_count == 0
        assert stored_second_version.complete is True
        assert stored_third_version.parse_issue_count == 1
        assert stored_third_version.complete is False
        assert first_file.status == "partial"
        assert second_file.status == "complete"
        assert third_file.status == "partial"

    repeated_report = backfill_jsonb_payloads(factory, limit=1)
    assert repeated_report.repaired_payload_count == 0
    assert repeated_report.recomputed_version_count == 0
    assert repeated_report.updated_snapshot_file_count == 0


if __name__ == "__main__":
    pytest_bazel.main()
