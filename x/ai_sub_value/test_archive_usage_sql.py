"""Exercise archive SQL against an isolated raw-only schema."""

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep @pypi//psycopg
from datetime import UTC, datetime
from hashlib import md5
from typing import Any

import pytest_bazel
from sqlalchemy import create_engine, text

from util.bazel.runfiles import get_required_path, own_repo_rlocation

pytest_plugins = ("util.testing.postgres_fixtures",)

_SAMPLE_SQL = get_required_path(own_repo_rlocation("x/ai_sub_value/archive-token-sample.sql")).read_text()
_PARTIAL_SQL = get_required_path(own_repo_rlocation("x/ai_sub_value/archive-partial-records.sql")).read_text()

_DDL = (
    """CREATE TEMP TABLE sessions (
        id uuid PRIMARY KEY,
        machine_id text NOT NULL,
        harness text NOT NULL,
        last_activity_at timestamptz NOT NULL
    ) ON COMMIT DROP""",
    """CREATE TEMP TABLE file_versions (
        id uuid PRIMARY KEY,
        session_id uuid NOT NULL,
        parent_version_id uuid,
        shared_prefix_record_count integer NOT NULL,
        record_count integer NOT NULL,
        complete boolean NOT NULL,
        parse_issue_count integer NOT NULL,
        first_seen_at timestamptz NOT NULL,
        last_seen_at timestamptz NOT NULL
    ) ON COMMIT DROP""",
    """CREATE TEMP TABLE file_version_records (
        file_version_id uuid NOT NULL,
        ordinal integer NOT NULL,
        payload_id uuid NOT NULL,
        PRIMARY KEY (file_version_id, ordinal)
    ) ON COMMIT DROP""",
    """CREATE TEMP TABLE record_payloads (
        id uuid PRIMARY KEY,
        sha256 text NOT NULL,
        parser_version text NOT NULL,
        raw_bytes bytea NOT NULL,
        parse_error text,
        timestamp timestamptz,
        kind text
    ) ON COMMIT DROP""",
    """CREATE TEMP TABLE usage_observations (
        payload_id uuid NOT NULL,
        observation_index integer NOT NULL,
        source_field text NOT NULL,
        counter_kind text NOT NULL,
        model text,
        response_id text,
        message_id text,
        turn_id text,
        input_tokens bigint,
        output_tokens bigint,
        cache_read_input_tokens bigint,
        cache_write_input_tokens bigint,
        reasoning_output_tokens bigint,
        total_tokens bigint,
        PRIMARY KEY (payload_id, observation_index)
    ) ON COMMIT DROP""",
)


def test_archive_usage_sql_reconstructs_partial_versions_without_counter_multiplication(
    postgres_admin_url: str,
) -> None:
    engine = create_engine(postgres_admin_url)
    try:
        with engine.connect() as connection, connection.begin():
            for statement in _DDL:
                connection.exec_driver_sql(statement)

            connection.exec_driver_sql(
                """INSERT INTO sessions (id, machine_id, harness, last_activity_at) VALUES
                ('00000000-0000-0000-0000-000000000001', 'machine-a', 'claude', '2026-01-01T00:00:00Z'),
                ('00000000-0000-0000-0000-000000000002', 'machine-a', 'claude', '2026-01-02T00:00:00Z')"""
            )
            connection.exec_driver_sql(
                """INSERT INTO file_versions (
                    id, session_id, parent_version_id, shared_prefix_record_count,
                    record_count, complete, parse_issue_count, first_seen_at, last_seen_at
                ) VALUES
                ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-000000000001',
                 NULL, 0, 1, TRUE, 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
                ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-000000000002',
                 NULL, 0, 3, TRUE, 0, '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z'),
                ('00000000-0000-0000-0000-000000000013', '00000000-0000-0000-0000-000000000002',
                 '00000000-0000-0000-0000-000000000012', 3, 5, FALSE, 1,
                 '2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z')"""
            )

            records: list[dict[str, Any]] = [
                {
                    "id": "00000000-0000-0000-0000-000000000101",
                    "raw_bytes": b'{"type":"assistant"}',
                    "parse_error": None,
                    "timestamp": datetime(2026, 1, 3, 0, 0, tzinfo=UTC),
                    "kind": "assistant",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000102",
                    "raw_bytes": b'{"type":',
                    "parse_error": "JSONDecodeError: invalid JSON",
                    "timestamp": None,
                    "kind": None,
                },
                {
                    "id": "00000000-0000-0000-0000-000000000103",
                    # This is valid JSON; decoding its string yields a NUL codepoint.
                    "raw_bytes": b'{"type":"event_msg","text":"\\u0000"}',
                    "parse_error": None,
                    "timestamp": datetime(2026, 1, 3, 0, 0, 2, tzinfo=UTC),
                    "kind": "event_msg",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000104",
                    "raw_bytes": b'{"type":"turn_context"}',
                    "parse_error": None,
                    "timestamp": datetime(2026, 1, 3, 0, 0, 3, tzinfo=UTC),
                    "kind": "turn_context",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000105",
                    "raw_bytes": b'{"type":"token_usage_record"}',
                    "parse_error": None,
                    "timestamp": datetime(2026, 1, 3, 0, 0, 4, tzinfo=UTC),
                    "kind": "token_usage_record",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000106",
                    "raw_bytes": b'{"type":"assistant","old":true}',
                    "parse_error": None,
                    "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                    "kind": "assistant",
                },
            ]
            connection.execute(
                text(
                    """INSERT INTO record_payloads (
                        id, sha256, parser_version, raw_bytes, parse_error, timestamp, kind
                    ) VALUES (:id, :sha256, 'jsonl-v2', :raw_bytes, :parse_error, :timestamp, :kind)"""
                ),
                [{**record, "sha256": f"{index:064x}"} for index, record in enumerate(records, start=1)],
            )
            connection.exec_driver_sql(
                """INSERT INTO file_version_records (file_version_id, ordinal, payload_id) VALUES
                ('00000000-0000-0000-0000-000000000011', 0, '00000000-0000-0000-0000-000000000106'),
                ('00000000-0000-0000-0000-000000000012', 0, '00000000-0000-0000-0000-000000000101'),
                ('00000000-0000-0000-0000-000000000012', 1, '00000000-0000-0000-0000-000000000102'),
                ('00000000-0000-0000-0000-000000000012', 2, '00000000-0000-0000-0000-000000000103'),
                ('00000000-0000-0000-0000-000000000013', 3, '00000000-0000-0000-0000-000000000104'),
                ('00000000-0000-0000-0000-000000000013', 4, '00000000-0000-0000-0000-000000000105')"""
            )

            connection.exec_driver_sql(
                """INSERT INTO usage_observations (
                    payload_id, observation_index, source_field, counter_kind, model,
                    response_id, message_id, turn_id, input_tokens, output_tokens,
                    cache_read_input_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, total_tokens
                ) VALUES
                ('00000000-0000-0000-0000-000000000101', 0, 'message.usage', 'message',
                 'claude-sonnet-4-5', 'response-secret-101', 'message-secret-101', NULL,
                 10, 4, 2, NULL, NULL, 14),
                ('00000000-0000-0000-0000-000000000105', 0, 'payload.usage', 'response',
                 'gpt-5-codex', 'response-secret-105', NULL, 'turn-secret-105',
                 NULL, 6, NULL, 1, 1, 7),
                ('00000000-0000-0000-0000-000000000105', 1, 'payload.turn_token_usage', 'turn',
                 'gpt-5-codex', 'response-secret-105', NULL, 'turn-secret-105',
                 20, 6, 3, 1, 1, 31)"""
            )

            samples = [row[0] for row in connection.exec_driver_sql(_SAMPLE_SQL)]
            assert [sample["ordinal"] for sample in samples] == [0, 1, 2, 3, 4]
            assert {sample["version_status"] for sample in samples} == {"partial"}
            assert {sample["version_parse_issue_count"] for sample in samples} == {1}
            assert samples[1]["parse_error"] == "JSONDecodeError: invalid JSON"
            assert samples[2]["kind"] == "event_msg"
            assert samples[2]["parse_error"] is None
            assert samples[2]["observations"] == []
            assert samples[3]["kind"] == "turn_context"
            assert samples[3]["observations"] == []
            assert "model" not in samples[3]
            assert samples[4]["kind"] == "token_usage_record"
            assert [row["observation_index"] for row in samples[4]["observations"]] == [0, 1]
            assert samples[4]["observations"][0]["model"] == "gpt-5-codex"
            assert samples[4]["observations"][0]["input_tokens"] is None
            assert samples[4]["observations"][0]["output_tokens"] == 6
            assert samples[4]["observations"][1]["cache_write_input_tokens"] == 1
            assert samples[0]["observations"][0]["response"] == md5(b"response-secret-101").hexdigest()
            assert "response-secret-101" not in repr(samples)
            assert "message-secret-101" not in repr(samples)

            reports = {row[0]["issue"]: row[0] for row in connection.exec_driver_sql(_PARTIAL_SQL)}
            assert set(reports) == {"readable", "JSONDecodeError"}
            assert reports["readable"]["machine"] == "machine-a"
            assert reports["readable"]["harness"] == "claude"
            assert reports["readable"]["physical_records"] == 4
            assert reports["readable"]["readable_records"] == 4
            assert reports["readable"]["parse_error_records"] == 0
            # Payload 105 has two counter observations but is one physical record.
            assert reports["readable"]["usage_bearing_records"] == 2
            assert reports["readable"]["readable_usage_bearing_records"] == 2
            assert reports["JSONDecodeError"]["physical_records"] == 1
            assert reports["JSONDecodeError"]["readable_records"] == 0
            assert reports["JSONDecodeError"]["usage_bearing_records"] == 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
