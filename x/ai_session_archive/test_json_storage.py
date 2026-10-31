# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import base64
import json
import time
from pathlib import Path

import pytest_bazel
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import RecordPayloadRow, SessionRow, SnapshotFileRow, UsageObservationRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.queries import list_events, list_sessions, list_versions
from x.ai_session_archive.record_json import parse_record_json
from x.ai_session_archive.testing import _backup, _new_repository
from x.ai_session_archive.usage_observations import UsageCounterKind
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_json_null_and_syntax_errors_keep_distinct_parse_state() -> None:
    json_null = parse_record_json(b"null")
    invalid = parse_record_json(b'{"type": }')

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
        b'{"type":"assistant","timestamp":"2026-01-01T00:00:00Z","nested":{"nul":"\\u0000"}}\n',
        b'{"type":"user","title":"good title","message":{"content":"good title"}}\n',
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
        assert events.data[1].parsed == {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "nested": {"nul": "\x00"},
        }
        assert events.data[3].parsed is None
        assert events.data[3].parse_error is None
        assert events.data[4].parsed is None
        assert events.data[4].parse_error is not None
        assert events.data[5].parsed == {"type": "assistant", "message": {"content": "following readable record"}}

    nul_payload = next(payload for payload in payloads if payload.raw_bytes == lines[1])
    json_null_payload = next(payload for payload in payloads if payload.raw_bytes == lines[3])
    invalid_payload = next(payload for payload in payloads if payload.raw_bytes == lines[4])
    assert parse_record_json(nul_payload.raw_bytes).parsed == {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "nested": {"nul": "\x00"},
    }
    assert nul_payload.parse_error is None
    assert json_null_payload.raw_bytes == lines[3]
    assert json_null_payload.parse_error is None
    assert invalid_payload.raw_bytes == lines[4]
    assert invalid_payload.parse_error is not None
    assert "parsed" not in {column["name"] for column in inspect(postgres_engine).get_columns("record_payloads")}
    with postgres_engine.connect() as connection:
        payload_columns = connection.exec_driver_sql(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'record_payloads'"
        ).mappings()
        payload_column_types: dict[str, str] = {
            str(column["column_name"]): str(column["data_type"]) for column in payload_columns
        }
    assert payload_column_types["parse_error"] == "text"
    assert payload_column_types["kind"] == "text"

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
    assert http_events[1]["parsed"]["nested"]["nul"] == "\x00"
    assert http_events[3]["parsed"] is None
    assert http_events[3]["parse_error"] is None
    assert http_events[4]["parsed"] is None
    assert http_events[4]["parse_error"] is not None
    assert http_events[5]["parsed"]["message"]["content"] == "following readable record"


def test_usage_observations_preserve_source_blocks_and_deduplicate_payloads(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    repository, environment = _new_repository(tmp_path)
    home = tmp_path / "home"
    claude_root = home / ".claude"
    codex_root = home / ".codex"
    claude_transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    codex_transcript = codex_root / "sessions" / "2026" / "session.jsonl"
    claude_transcript.parent.mkdir(parents=True)
    codex_transcript.parent.mkdir(parents=True)
    message = (
        b'{"type":"assistant","message":{"id":"message-1","model":"claude-sonnet-5-5",'
        b'"usage":{"input_tokens":120,"output_tokens":30,"cache_read_input_tokens":50,'
        b'"cache_creation_input_tokens":7,"output_tokens_details":{"thinking_tokens":4}}}}\n'
    )
    result = (
        b'{"type":"result","usage":{"input_tokens":400,"output_tokens":90,"cache_read_input_tokens":'
        b'200,"cache_creation_input_tokens":10,"total_tokens":490},"modelUsage":{'
        b'"claude-sonnet-5-5":{"inputTokens":360,"outputTokens":80,"cacheReadInputTokens":180,'
        b'"cacheCreationInputTokens":8,"thinkingTokens":2},"claude-haiku-4-5":'
        b'{"inputTokens":40,"outputTokens":10}}}\n'
    )
    codex_response = (
        b'{"type":"token_usage_record","payload":{"response_id":"response-1","turn_id":"turn-1",'
        b'"thread_id":"thread-1","usage":{"input_tokens":21,"cached_input_tokens":10,'
        b'"cache_write_input_tokens":2,"output_tokens":5,"reasoning_output_tokens":1,"total_tokens":26},'
        b'"turn_token_usage":{"input_tokens":21,"cached_input_tokens":10,"cache_write_input_tokens":2,'
        b'"output_tokens":5,"reasoning_output_tokens":1,"total_tokens":26},'
        b'"thread_token_usage":{"input_tokens":21,"cached_input_tokens":10,"cache_write_input_tokens":2,'
        b'"output_tokens":5,"reasoning_output_tokens":1,"total_tokens":26}}}\n'
    )
    codex_token_count = (
        b'{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":'
        b'{"input_tokens":21,"cached_input_tokens":10,"cache_write_input_tokens":2,"output_tokens":5,'
        b'"reasoning_output_tokens":1,"total_tokens":26},"total_token_usage":{"input_tokens":42,'
        b'"cached_input_tokens":20,"cache_write_input_tokens":4,"output_tokens":10,'
        b'"reasoning_output_tokens":2,"total_tokens":52}}}}\n'
    )
    claude_transcript.write_bytes(message + message + result)
    codex_transcript.write_bytes(codex_response + codex_response + codex_token_count)
    _backup(environment, home)

    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    source = ArchiveSource(
        machine_id="usage-test",
        display_name="Usage test",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)), HarnessRoot(Harness.CODEX, str(codex_root))),
    )
    ingestor = SnapshotIngestor(factory, source)
    ingestor.ingest()
    with factory() as db:
        before = list(
            db.scalars(
                select(UsageObservationRow).order_by(
                    UsageObservationRow.payload_id, UsageObservationRow.observation_index
                )
            )
        )
    ingestor.ingest()
    with factory() as db:
        after = list(
            db.scalars(
                select(UsageObservationRow).order_by(
                    UsageObservationRow.payload_id, UsageObservationRow.observation_index
                )
            )
        )
        payload_by_raw = {row.raw_bytes: row for row in db.scalars(select(RecordPayloadRow))}

    assert len(before) == len(after) == 9
    assert [(row.payload_id, row.observation_index) for row in before] == [
        (row.payload_id, row.observation_index) for row in after
    ]
    assert len([row for row in before if row.payload_id == payload_by_raw[message].id]) == 1
    assert len([row for row in before if row.payload_id == payload_by_raw[codex_response].id]) == 3
    by_source = {(row.source_field, row.counter_kind): row for row in before}
    claude_message = by_source[("message.usage", UsageCounterKind.MESSAGE)]
    assert claude_message.model == "claude-sonnet-5-5"
    assert claude_message.message_id == "message-1"
    assert claude_message.input_tokens == 120
    assert claude_message.cache_write_input_tokens == 7
    assert claude_message.reasoning_output_tokens == 4
    claude_result = by_source[("usage", UsageCounterKind.RESULT)]
    assert claude_result.total_tokens == 490
    model_result = by_source[('modelUsage["claude-sonnet-5-5"]', UsageCounterKind.MODEL_RESULT)]
    assert model_result.model == "claude-sonnet-5-5"
    assert model_result.input_tokens == 360
    assert model_result.cache_read_input_tokens == 180
    assert model_result.reasoning_output_tokens == 2
    assert model_result.total_tokens is None
    haiku_result = by_source[('modelUsage["claude-haiku-4-5"]', UsageCounterKind.MODEL_RESULT)]
    assert haiku_result.input_tokens == 40
    assert haiku_result.cache_read_input_tokens is None
    assert by_source[("payload.usage", UsageCounterKind.RESPONSE)].response_id == "response-1"
    assert by_source[("payload.turn_token_usage", UsageCounterKind.TURN)].turn_id == "turn-1"
    assert by_source[("payload.thread_token_usage", UsageCounterKind.THREAD)].input_tokens == 21
    assert by_source[("payload.info.last_token_usage", UsageCounterKind.LAST_RESPONSE)].input_tokens == 21
    session_total = by_source[("payload.info.total_token_usage", UsageCounterKind.SESSION)]
    assert session_total.input_tokens == 42
    assert session_total.total_tokens == 52
    assert session_total.model is None


if __name__ == "__main__":
    pytest_bazel.main()
