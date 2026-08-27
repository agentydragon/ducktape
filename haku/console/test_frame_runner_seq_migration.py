"""The v3 frame log rejects pre-cutover kinds and deduplicates runner positions."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)


def _conversation(conn: Connection, operator_id: UUID) -> UUID:
    """The thread a session runs, which every session carries and owes."""
    conversation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at) VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    return conversation_id


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
                session_id, operator_id, conversation_id, bridge_token_fingerprint,
                bridge_connected_at, lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, :fingerprint, :n, :n, :n, :n)
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "conversation_id": _conversation(conn, operator_id),
            "fingerprint": b"digest",
            "n": _NOW,
        },
    )
    return session_id


def _frame(conn: Connection, session_id: UUID, kind: str, **extra: object) -> None:
    columns = {"session_id": session_id, "direction": "from_agent", "kind": kind, "payload": "{}", **extra}
    named = ", ".join(columns)
    conn.execute(
        text(
            f"INSERT INTO session_frames ({named}, created_at, updated_at) "
            f"VALUES ({', '.join(f':{name}' for name in columns)}, :n, :n)"
        ),
        columns | {"n": _NOW},
    )


def test_a_console_write_without_a_runner_number_still_records(db_url: str) -> None:
    """Only runner-origin frames have a runner position; Console writes honestly leave it NULL."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            _frame(conn, session_id, "harness_frame")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT runner_seq FROM session_frames")).scalar_one() is None
    finally:
        engine.dispose()


def test_one_session_cannot_hold_the_same_runner_number_twice(db_url: str) -> None:
    """Runner position is the identity even for native frames with no payload-level id."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            _frame(conn, session_id, "harness_frame", runner_seq=7)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _frame(conn, session_id, "harness_frame", runner_seq=7)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
