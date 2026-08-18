"""0082 gives `session_events` the conversation as a column and lets `session_id` be absent.

What is worth stating in Postgres rather than in Python: that the backfill reaches every row through
the session it names, that the two arms of the provenance union now differ in whether they may omit
a session, and that the widened CHECK admits exactly the five new kinds and no invented sixth.
"""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 18, tzinfo=datetime.UTC)
_BEFORE = "0081"

_NEW_KINDS = ("session_provisioning", "session_ended", "setup_narration", "turn_started", "turn_ended")


def _conversation(conn: Connection) -> tuple[UUID, UUID]:
    operator_id, conversation_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
        {"id": conversation_id, "o": operator_id, "n": _NOW},
    )
    return operator_id, conversation_id


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


def _authored(conn: Connection, kind: str, *, session_id: UUID | None, body: object = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO session_events (
                session_id, turn_id, kind, provenance,
                source_first_frame_seq, source_last_frame_seq, call_id, body, created_at
            ) VALUES (:session_id, NULL, :kind, 'authored', NULL, NULL, NULL, CAST(:body AS jsonb), :n)
            """
        ),
        {"session_id": session_id, "kind": kind, "body": json.dumps(body or {}), "n": _NOW},
    )


def test_every_event_takes_the_conversation_of_the_session_that_wrote_it(db_url: str) -> None:
    """Two threads, so a backfill that assigned one conversation to everything would be caught.

    Nothing can be missed: `session_events.session_id` was `NOT NULL` with a cascading foreign key
    before this ran, and `sessions.conversation_id` has been `NOT NULL` since `0072`.
    """
    apply_migrations(db_url, _BEFORE)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id, first = _conversation(conn)
            _, second = _conversation(conn)
            here = _session(conn, operator_id, first)
            there = _session(conn, operator_id, second)
            _authored(conn, "lease_expired", session_id=here, body={"reason": "holder_gone", "last_holder": None})
            _authored(conn, "lease_expired", session_id=there, body={"reason": "unadopted", "last_holder": None})

        apply_migrations(db_url)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT session_id, conversation_id FROM session_events ORDER BY event_seq")).all()
            assert [(row.session_id, row.conversation_id) for row in rows] == [(here, first), (there, second)]
    finally:
        engine.dispose()


def test_an_authored_row_may_name_no_session_and_a_folded_one_may_not(db_url: str) -> None:
    """The point of the re-key, and the guard that keeps the loss narrow.

    A prompt accepted before any sandbox exists has no session to name. A row folded out of frames
    always does, because the frames are a session's.
    """
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _authored(conn, "session_provisioning", session_id=None)

        with pytest.raises(IntegrityError, match="ck_session_events_frame_range_session"), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO session_events (
                        session_id, turn_id, kind, provenance,
                        source_first_frame_seq, source_last_frame_seq, call_id, body, created_at
                    ) VALUES (NULL, NULL, 'reasoning', 'frame_range', 1, 1, NULL, '{}'::jsonb, :n)
                    """
                ),
                {"n": _NOW},
            )
    finally:
        engine.dispose()


def test_the_widened_check_admits_the_five_new_kinds_and_nothing_else(db_url: str) -> None:
    """A kind the CHECK admitted but no enum knew would fail on read rather than on write —
    `TextBackedStrEnumUnionColumn` raises on an unknown value — so the two lists are one fact."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            for kind in _NEW_KINDS:
                _authored(conn, kind, session_id=None)

        with pytest.raises(IntegrityError, match="ck_session_events_kind"), engine.begin() as conn:
            _authored(conn, "session_hiccuped", session_id=None)

        with engine.connect() as conn:
            stored = conn.execute(text("SELECT kind FROM session_events ORDER BY event_seq")).scalars().all()
            assert tuple(stored) == _NEW_KINDS
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
