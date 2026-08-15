"""The `claude_chat_*` compatibility views, against a real Postgres.

Migration `0040` renamed six tables and left each old name behind as an auto-updatable view, so a
replica on the previous image keeps working for the length of a `maxUnavailable: 0` roll. Nothing
else covers that: every other test in this tree has the new names on both ends of the wire, so it
would pass with the views missing entirely.

Raw SQL, and deliberately: the statements here are the ones the *previous* release emits, which is
exactly what the ORM in this release can no longer express. The `ON CONFLICT` case is the one worth
the file — arbiter inference against a partial index is the write most likely to be rejected on a
view rather than a table.

CLEANUP(added 2026-08-15): delete this file with the contract migration that drops the views.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Engine, create_engine, text

from haku.console.database_migrate import sync_database_url

_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)


@pytest.fixture
def engine(migrated_db_url: str) -> Iterator[Engine]:
    engine = create_engine(sync_database_url(migrated_db_url))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_id(engine: Engine) -> UUID:
    """One session and its operator, written the way the previous release writes them."""
    operator_id, session_id = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
            {"id": operator_id, "n": _NOW},
        )
        conn.execute(
            text("""
            INSERT INTO claude_chat_sessions (
                session_id, operator_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, 'spa', 'ready', :fingerprint, :n, :n, :n)
            """),
            {"session_id": session_id, "operator_id": operator_id, "fingerprint": b"fingerprint", "n": _NOW},
        )
    return session_id


def test_the_old_names_still_accept_the_previous_release_s_writes(engine: Engine, session_id: UUID) -> None:
    message_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO claude_chat_messages (
                message_id, session_id, role, status, content, tool_uses, created_at, updated_at
            ) VALUES (:message_id, :session_id, 'user', 'pending', 'hello', '[]'::jsonb, :n, :n)
            """),
            {"message_id": message_id, "session_id": session_id, "n": _NOW},
        )
        conn.execute(
            text("UPDATE claude_chat_messages SET status = 'complete' WHERE message_id = :message_id"),
            {"message_id": message_id},
        )

    with engine.connect() as conn:
        # Read back under the *new* name: the view and the table are one set of rows, not two.
        assert (
            conn.execute(
                text("SELECT status FROM session_messages WHERE message_id = :message_id"), {"message_id": message_id}
            ).scalar_one()
            == "complete"
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM sessions WHERE session_id = :session_id"), {"session_id": session_id}
            ).scalar_one()
            == 1
        )


def test_a_replayed_frame_is_still_refused_through_the_old_name(engine: Engine, session_id: UUID) -> None:
    """`ON CONFLICT` inference over `uq_session_frames_uid`, emitted against the view.

    `frame_seq` is `GENERATED ALWAYS`, so this also pins that the base table's identity is applied
    to an insert that never names the column.
    """
    insert = text("""
        INSERT INTO claude_chat_frames (
            session_id, direction, kind, payload, partial, frame_uid, created_at, updated_at
        ) VALUES (:session_id, 'from_agent', 'assistant', :payload, false, 'msg_1', :n, :n)
        ON CONFLICT (session_id, frame_uid) WHERE frame_uid IS NOT NULL DO NOTHING
    """)
    parameters = {"session_id": session_id, "payload": json.dumps({"type": "assistant"}), "n": _NOW}

    with engine.begin() as conn:
        assert conn.execute(insert, parameters).rowcount == 1
        assert conn.execute(insert, parameters).rowcount == 0

    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT frame_seq FROM session_frames WHERE session_id = :session_id"), {"session_id": session_id}
            ).scalar_one()
            > 0
        )


def test_the_old_name_locks_and_updates_the_row_the_new_name_reads(engine: Engine, session_id: UUID) -> None:
    """`SELECT … FOR UPDATE` is how every session mutation in the previous release starts."""
    with engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT status FROM claude_chat_sessions WHERE session_id = :session_id FOR UPDATE"),
                {"session_id": session_id},
            ).scalar_one()
            == "ready"
        )
        conn.execute(
            text("UPDATE claude_chat_sessions SET status = 'closed' WHERE session_id = :session_id"),
            {"session_id": session_id},
        )

    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT status FROM sessions WHERE session_id = :session_id"), {"session_id": session_id}
            ).scalar_one()
            == "closed"
        )


if __name__ == "__main__":
    pytest_bazel.main()
