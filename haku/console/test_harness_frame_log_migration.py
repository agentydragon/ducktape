"""The v3 frame-log cutover resets sessions and installs the bridge vocabulary."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    return operator_id


def _conversation(conn: Connection, operator_id: UUID) -> UUID:
    conversation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at) "
            "VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    return conversation_id


def _session(conn: Connection, operator_id: UUID, conversation_id: UUID) -> UUID:
    session_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, 'ready', :fingerprint, :n, :n, :n)
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


def _frame(conn: Connection, session_id: UUID, kind: str, *, runner_seq: int | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO session_frames (
                session_id, direction, kind, payload, runner_seq, created_at, updated_at
            ) VALUES (:session_id, 'from_agent', :kind, '{}', :runner_seq, :n, :n)
            """
        ),
        {"session_id": session_id, "kind": kind, "runner_seq": runner_seq, "n": _NOW},
    )


def test_cutover_preserves_thread_identity_and_replaces_the_frame_contract(db_url: str) -> None:
    apply_migrations(db_url, "0088")
    engine = create_engine(sync_database_url(db_url))
    operator_id: UUID
    conversation_id: UUID
    attachment_id = uuid4()
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            conversation_id = _conversation(conn, operator_id)
            session_id = _session(conn, operator_id, conversation_id)
            conn.execute(
                text(
                    "INSERT INTO chat_attachment "
                    "(attachment_id, conversation_id, surface, address, attached_at) "
                    "VALUES (:id, :conversation, 'matrix', '!room:example.org', :n)"
                ),
                {"id": attachment_id, "conversation": conversation_id, "n": _NOW},
            )
            _frame(conn, session_id, "assistant", runner_seq=7)

        # Keep this PR1 cutover test independent of the later conversation-identity migration.
        apply_migrations(db_url, "0090")

        with engine.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM conversation WHERE conversation_id = :id"), {"id": conversation_id}
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM chat_attachment WHERE attachment_id = :id"), {"id": attachment_id}
                ).scalar_one()
                == 1
            )
            assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM session_frames")).scalar_one() == 0
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'session_frames' AND column_name = 'frame_uid'"
                    )
                ).scalar_one()
                == 0
            )
            new_session = _session(conn, operator_id, conversation_id)
            _frame(conn, new_session, "harness_frame", runner_seq=7)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _frame(conn, new_session, "harness_frame", runner_seq=7)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _frame(conn, new_session, "assistant", runner_seq=8)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
