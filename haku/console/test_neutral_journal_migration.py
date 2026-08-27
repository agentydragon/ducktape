"""0106's neutral-journal state lands expand-only on a database with live sessions.

Seeds a session at 0105, upgrades to head, and pins what the consumer relies on: the cursor
arriving at its zero, the per-session uniqueness of runner-minted identities, and the inbox's
state-machine checks.

Temporary per `AGENTS.md` § "Do not keep tests for old migrations": delete once the chain is
roughly five revisions past 0106 (~0111).
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC)


def _session(conn: Connection) -> tuple[UUID, UUID]:
    operator_id, conversation_id, session_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at)"
            " VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, :fingerprint, :n, :n, :n)
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "fingerprint": session_id.bytes,
            "n": _NOW,
        },
    )
    return conversation_id, session_id


def _open_item(conn: Connection, conversation_id: UUID, session_id: UUID, runner_item_id: UUID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO conversation_item (
                item_id, conversation_id, session_id, item_type, status, opened_seq,
                text, runner_item_id, created_at, updated_at
            ) VALUES (:id, :conversation_id, :session_id, 'message', 'open', 1, '',
                      :runner_item_id, :n, :n)
            """
        ),
        {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "session_id": session_id,
            "runner_item_id": runner_item_id,
            "n": _NOW,
        },
    )


def test_0106_defaults_the_cursor_on_preexisting_sessions(db_url: str) -> None:
    apply_migrations(db_url, "0105")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _conversation_id, session_id = _session(conn)

        apply_migrations(db_url)

        with engine.begin() as conn:
            cursor = conn.execute(
                text("SELECT acked_batch_seq FROM sessions WHERE session_id = :id"), {"id": session_id}
            ).scalar_one()
            assert cursor == 0
    finally:
        engine.dispose()


def test_0106_rejects_a_reused_runner_item_id(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            conversation_id, session_id = _session(conn)
            runner_item_id = uuid4()
            _open_item(conn, conversation_id, session_id, runner_item_id)
            with pytest.raises(IntegrityError, match="uq_conversation_item_runner"), conn.begin_nested():
                _open_item(conn, conversation_id, session_id, runner_item_id)
    finally:
        engine.dispose()


def test_0106_inbox_admission_is_paired_and_text_nonempty(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            conversation_id, _session_id = _session(conn)

            def submit(text_value: str, admitted_at: datetime.datetime | None) -> None:
                conn.execute(
                    text(
                        """
                        INSERT INTO submitted_prompt (
                            prompt_id, conversation_id, text, origin, submitted_at, admitted_at
                        ) VALUES (:prompt_id, :conversation_id, :text, '{"kind": "spa"}'::jsonb,
                                  :n, :admitted_at)
                        """
                    ),
                    {
                        "prompt_id": uuid4(),
                        "conversation_id": conversation_id,
                        "text": text_value,
                        "admitted_at": admitted_at,
                        "n": _NOW,
                    },
                )

            submit("hello", None)
            # Admission without the item it materialised is the pair the check forbids.
            with pytest.raises(IntegrityError, match="ck_submitted_prompt_admission_pair"), conn.begin_nested():
                submit("hello", _NOW)
            with pytest.raises(IntegrityError, match="ck_submitted_prompt_text_nonempty"), conn.begin_nested():
                submit(" ", None)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
