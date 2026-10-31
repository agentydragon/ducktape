# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
import json
import time
from pathlib import Path

import pytest_bazel
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import RecordPayloadRow, SessionRow, SnapshotFileRow
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
    assert json_null.parse_error is None
    assert invalid.parsed is None
    assert invalid.parse_error is not None


def test_ingest_and_http_round_trip_json_nul_and_parse_issue(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, environment = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    lines = [
        b'{"type":"session_meta","payload":{"id":"json-storage-session"},"cwd":"/home/archive-test"}\n',
        b'{"type":"user","title":"good title","message":{"content":"good title"}}\n',
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:00Z","nested":{"nul":"\\u0000"}}\n',
        b"null\n",
        b'{"type": }\n',
        b'{"type":"assistant","message":{"content":"following readable record"}}\n',
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
        assert session_row.cwd == "/home/archive-test"
        assert session_row.parent_native_session_id is None
        assert version.parse_issue_count == 1
        assert version.complete is False
        assert len(events.data) == 6
        assert events.data[2].parsed == {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "nested": {"nul": "\x00"},
        }
        assert events.data[3].parsed is None
        assert events.data[3].parse_error is None
        assert events.data[4].parsed is None
        assert events.data[4].parse_error is not None
        assert events.data[5].parsed == {"type": "assistant", "message": {"content": "following readable record"}}

    nul_payload = next(payload for payload in payloads if payload.raw_bytes == lines[2])
    json_null_payload = next(payload for payload in payloads if payload.raw_bytes == lines[3])
    invalid_payload = next(payload for payload in payloads if payload.raw_bytes == lines[4])
    assert isinstance(nul_payload.parsed, dict)
    assert nul_payload.parsed["nested"]["nul"] == "\x00"
    assert nul_payload.parse_error is None
    assert json_null_payload.parsed is None
    assert json_null_payload.parse_error is None
    assert invalid_payload.parsed is None
    assert invalid_payload.parse_error is not None
    with postgres_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT pg_typeof(parsed)::text FROM record_payloads WHERE raw_bytes = %s", (lines[2],)
            ).scalar_one()
            == "json"
        )
        assert not connection.exec_driver_sql(
            "SELECT parsed IS NULL FROM record_payloads WHERE raw_bytes = %s", (lines[3],)
        ).scalar_one()
        assert connection.exec_driver_sql(
            "SELECT parsed IS NULL FROM record_payloads WHERE raw_bytes = %s", (lines[4],)
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
    assert b"\x00" not in response.content
    http_events = response.json()["data"]
    assert http_events[2]["parsed"]["nested"]["nul"] == "\x00"
    assert http_events[3]["parsed"] is None
    assert http_events[3]["parse_error"] is None
    assert http_events[4]["parsed"] is None
    assert http_events[4]["parse_error"] is not None
    assert http_events[5]["parsed"]["message"]["content"] == "following readable record"


if __name__ == "__main__":
    pytest_bazel.main()
