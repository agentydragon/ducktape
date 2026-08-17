"""0068 drops what three releases stopped writing, and rewrites the one thing it will not destroy.

Destroying conversation data is authorized here (operator, 2026-08-17), which is what lets the
activity rows and the `partial` frames go outright. A stored tool result is the exception: its two
older spellings have to leave the union, because `ToolResultBody` is parsed on every SPA read, but
deleting the rows would blank an old transcript for nothing — so they are rewritten to the `text`
shape carrying what the reader used to render at read time.
"""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 17, tzinfo=datetime.UTC)
_BEFORE = "0067"


def _session(conn: Connection) -> UUID:
    operator_id, session_id, conversation_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    # `_BEFORE` is after `0064`, so the column exists here and `0072` will require it to be set.
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
        {"id": conversation_id, "o": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, 'spa', 'ready', :fingerprint, :n, :n, :n)
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "fingerprint": b"digest",
            "n": _NOW,
        },
    )
    return session_id


def _turn(conn: Connection, session_id: UUID) -> UUID:
    turn_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO session_turns (turn_id, session_id, first_frame_seq, started_at) "
            "VALUES (:turn_id, :session_id, 1, :n)"
        ),
        {"turn_id": turn_id, "session_id": session_id, "n": _NOW},
    )
    return turn_id


def _event(conn: Connection, session_id: UUID, turn_id: UUID, kind: str, body: object, call_id: str | None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO session_events (
                session_id, turn_id, kind, provenance,
                source_first_frame_seq, source_last_frame_seq, call_id, body, created_at
            ) VALUES (
                :session_id, :turn_id, :kind, 'frame_range', 1, 1, :call_id, CAST(:body AS jsonb), :n
            )
            """
        ),
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "kind": kind,
            "call_id": call_id,
            "body": json.dumps(body),
            "n": _NOW,
        },
    )


def _result(content: object) -> dict[str, object]:
    return {"content": content, "structured": None, "outcome": "succeeded"}


def test_the_two_older_result_spellings_are_rewritten_rather_than_deleted(db_url: str) -> None:
    """What each row keeps is what `session_views` used to compute for it at read time — the JSON
    rendering of the names or the payload — so the transcript reads the same after the collapse."""
    apply_migrations(db_url, _BEFORE)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            turn_id = _turn(conn, session_id)
            _event(conn, session_id, turn_id, "tool_call_completed", _result({"shape": "text", "text": "a.py"}), "t1")
            _event(
                conn,
                session_id,
                turn_id,
                "tool_call_completed",
                _result({"shape": "tool_references", "tool_names": ["Read", "Grep"]}),
                "t2",
            )
            _event(
                conn,
                session_id,
                turn_id,
                "tool_call_completed",
                _result({"shape": "opaque", "payload": {"unknown": True}}),
                "t3",
            )

        apply_migrations(db_url)

        with engine.connect() as conn:
            stored: dict[str, object] = {
                row.call_id: row.content
                for row in conn.execute(
                    text("SELECT call_id, body->'content' AS content FROM session_events ORDER BY call_id")
                )
            }

        assert stored == {
            "t1": {"shape": "text", "text": "a.py"},
            "t2": {"shape": "text", "text": '["Read", "Grep"]'},
            "t3": {"shape": "text", "text": '{"unknown": true}'},
        }
    finally:
        engine.dispose()


def test_the_activity_rows_go_and_the_kind_narrows_behind_them(db_url: str) -> None:
    """Order matters in one direction: a CHECK cannot be added over rows that violate it, so the
    delete has to precede the narrowing. The constraint afterwards is what proves it did."""
    apply_migrations(db_url, _BEFORE)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            turn_id = _turn(conn, session_id)
            _event(conn, session_id, turn_id, "activity_started", {"description": "compiling"}, None)
            _event(conn, session_id, turn_id, "activity_completed", {"description": "compiling"}, None)
            _event(conn, session_id, turn_id, "reasoning", {"summary": "thinking"}, None)

        apply_migrations(db_url)

        with engine.connect() as conn:
            assert [k for (k,) in conn.execute(text("SELECT kind FROM session_events"))] == ["reasoning"]
            assert (
                "activity_started"
                not in conn.execute(
                    text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_session_events_kind'")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_the_dropped_columns_are_gone_and_their_constraints_with_them(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE (table_name, column_name) IN "
                        "(('session_messages','tool_calls'),('session_messages','unpointable_reason'),"
                        "('session_frames','partial'))"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conname IN "
                        "('ck_session_messages_unpointable_reason','ck_session_messages_unpointable_exclusive')"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_session_frames_partial'")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
