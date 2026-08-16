"""The tool-call migration re-spells history, and leaves the column an old replica still reads."""

from __future__ import annotations

import datetime
import json
from uuid import uuid4

import pytest_bazel
from sqlalchemy import create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)
_CLAUDE_SHAPED = [
    {"tool_use_id": "toolu_01", "name": "Bash", "input": {"command": "true"}},
    {"tool_use_id": "toolu_02", "name": "Read", "input": {}},
]


def test_the_backfill_rewrites_stored_calls_without_disturbing_the_column_it_read(db_url: str) -> None:
    """Both halves matter. The rewrite is what makes historical rows readable at all — the model
    that validates `tool_calls` rejects Claude's keys — and leaving `tool_uses` exactly as it was
    is what lets a replica on the previous image keep serving through the roll that applies this.
    """
    apply_migrations(db_url, "0046")
    engine = create_engine(sync_database_url(db_url))
    operator_id, session_id, asked, said_nothing = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"
                ),
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
            for message_id, calls in ((asked, _CLAUDE_SHAPED), (said_nothing, [])):
                conn.execute(
                    text(
                        """
                        INSERT INTO session_messages (
                            message_id, session_id, role, status, content, tool_uses, created_at, updated_at
                        ) VALUES (
                            :message_id, :session_id, 'assistant', 'complete', '', CAST(:calls AS jsonb), :n, :n
                        )
                        """
                    ),
                    {"message_id": message_id, "session_id": session_id, "calls": json.dumps(calls), "n": _NOW},
                )

        apply_migrations(db_url)

        with engine.connect() as conn:
            rewritten, untouched = conn.execute(
                text("SELECT tool_calls, tool_uses FROM session_messages WHERE message_id = :id"), {"id": asked}
            ).one()
            assert rewritten == [
                {"call_id": "toolu_01", "tool_name": "Bash", "arguments": {"command": "true"}},
                {"call_id": "toolu_02", "tool_name": "Read", "arguments": {}},
            ], "the stored order is the order the agent asked in"
            assert untouched == _CLAUDE_SHAPED
            assert (
                conn.execute(
                    text("SELECT tool_calls FROM session_messages WHERE message_id = :id"), {"id": said_nothing}
                ).scalar_one()
                == []
            )
    finally:
        engine.dispose()


def test_a_row_written_by_a_replica_that_never_heard_of_the_column_is_still_legal(db_url: str) -> None:
    """The other direction of the same roll: an INSERT naming every column the previous image knew
    must still satisfy NOT NULL, which is what the server default is for."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    operator_id, session_id, message_id = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"
                ),
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
            conn.execute(
                text(
                    """
                    INSERT INTO session_messages (
                        message_id, session_id, role, status, content, tool_uses, created_at, updated_at
                    ) VALUES (:message_id, :session_id, 'assistant', 'complete', '', '[]'::jsonb, :n, :n)
                    """
                ),
                {"message_id": message_id, "session_id": session_id, "n": _NOW},
            )

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
