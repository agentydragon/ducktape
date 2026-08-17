"""The tool-call migration re-spells history, and leaves the column an old replica still reads.

The two tests about `tool_uses` stop at `0047`, the revision they are about: `0056` drops that
column, and a migration is a statement about the database at its own revision.
"""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)
_CLAUDE_SHAPED: list[dict[str, object]] = [
    {"tool_use_id": "toolu_01", "name": "Bash", "input": {"command": "true"}},
    {"tool_use_id": "toolu_02", "name": "Read", "input": {}},
]
_RESPELLED = [
    {"call_id": "toolu_01", "tool_name": "Bash", "arguments": {"command": "true"}},
    {"call_id": "toolu_02", "tool_name": "Read", "arguments": {}},
]


def _session(conn: Connection) -> UUID:
    operator_id, session_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, 'spa', 'ready', :fingerprint, :n, :n, :n)
            """
        ),
        {"session_id": session_id, "operator_id": operator_id, "fingerprint": b"fingerprint", "n": _NOW},
    )
    return session_id


def _message_with_tool_uses(conn: Connection, session_id: UUID, calls: list[dict[str, object]]) -> UUID:
    """An assistant row as a writer of its era left it: the calls in `tool_uses`, and — since `0058`
    requires it of every assistant row — the frame it was projected from."""
    message_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO session_messages (
                message_id, session_id, role, status, content, tool_uses,
                source_first_frame_seq, created_at, updated_at
            ) VALUES (
                :message_id, :session_id, 'assistant', 'complete', '', CAST(:calls AS jsonb), 1, :n, :n
            )
            """
        ),
        {"message_id": message_id, "session_id": session_id, "calls": json.dumps(calls), "n": _NOW},
    )
    return message_id


def test_the_backfill_rewrites_stored_calls_without_disturbing_the_column_it_read(db_url: str) -> None:
    """Both halves matter. The rewrite is what makes historical rows readable at all — the model
    that validates `tool_calls` rejects Claude's keys — and leaving `tool_uses` exactly as it was
    is what lets a replica on the previous image keep serving through the roll that applies this.
    """
    apply_migrations(db_url, "0046")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            asked = _message_with_tool_uses(conn, session_id, _CLAUDE_SHAPED)
            said_nothing = _message_with_tool_uses(conn, session_id, [])

        apply_migrations(db_url, "0047")

        with engine.connect() as conn:
            rewritten, untouched = conn.execute(
                text("SELECT tool_calls, tool_uses FROM session_messages WHERE message_id = :id"), {"id": asked}
            ).one()
            assert rewritten == _RESPELLED, "the stored order is the order the agent asked in"
            assert untouched == _CLAUDE_SHAPED
            assert (
                conn.execute(
                    text("SELECT tool_calls FROM session_messages WHERE message_id = :id"), {"id": said_nothing}
                ).scalar_one()
                == []
            )
    finally:
        engine.dispose()


def test_the_respelled_calls_outlive_the_column_they_were_read_from(db_url: str) -> None:
    """`0056` drops `tool_uses`, and this is what makes that safe to do: the rows it destroys were
    already carried across into `tool_calls`, so running the whole chain leaves the calls intact."""
    apply_migrations(db_url, "0046")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            asked = _message_with_tool_uses(conn, _session(conn), _CLAUDE_SHAPED)

        apply_migrations(db_url)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT tool_calls FROM session_messages WHERE message_id = :id"), {"id": asked}
                ).scalar_one()
                == _RESPELLED
            )
    finally:
        engine.dispose()


def test_a_row_written_by_a_replica_that_never_heard_of_the_column_is_still_legal(db_url: str) -> None:
    """The other direction of the same roll: an INSERT naming every column the previous image knew
    must still satisfy NOT NULL, which is what the server default is for."""
    apply_migrations(db_url, "0047")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            message_id = _message_with_tool_uses(conn, _session(conn), [])

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT tool_calls FROM session_messages WHERE message_id = :id"), {"id": message_id}
                ).scalar_one()
                == []
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
