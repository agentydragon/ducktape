"""`ck_session_events_prompt_origin`: an enqueued prompt has to say where it came from."""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 17, tzinfo=datetime.UTC)


def _session(conn: Connection) -> UUID:
    operator_id, session_id, conversation_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
        {"id": conversation_id, "o": operator_id, "n": _NOW},
    )
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


def _authored(conn: Connection, session_id: UUID, kind: str, body: object) -> None:
    conn.execute(
        text(
            """
            INSERT INTO session_events (
                session_id, turn_id, kind, provenance,
                source_first_frame_seq, source_last_frame_seq, call_id, body, created_at
            ) VALUES (:session_id, NULL, :kind, 'authored', NULL, NULL, NULL, CAST(:body AS jsonb), :n)
            """
        ),
        {"session_id": session_id, "kind": kind, "body": json.dumps(body), "n": _NOW},
    )


def _message(text_: str) -> dict[str, object]:
    return {"message_id": str(uuid4()), "text": text_}


def test_a_prompt_without_an_origin_can_no_longer_be_written(db_url: str) -> None:
    """The roll guard. The previous image keeps serving while this migration applies and names no
    origin, so what it writes has to fail at the table — a rejected insert is a failure the operator
    sees, where an accepted one is a session whose transcript never renders again."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)

        with engine.begin() as conn:
            _authored(conn, session_id, "prompt_enqueued", _message("named") | {"origin": {"kind": "spa"}})

        with pytest.raises(IntegrityError, match="ck_session_events_prompt_origin"), engine.begin() as conn:
            _authored(conn, session_id, "prompt_enqueued", _message("unnamed"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
