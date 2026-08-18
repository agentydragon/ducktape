"""0075 lets a session be inserted without naming `surface`."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_AT = datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC)


def _conversation(conn: Connection) -> UUID:
    operator_id, conversation_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO operators (operator_id, status, created_at, updated_at) "
            "VALUES (:operator_id, 'active', :at, :at)"
        ),
        {"operator_id": operator_id, "at": _AT},
    )
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, created_at) "
            "VALUES (:conversation_id, :operator_id, :at)"
        ),
        {"conversation_id": conversation_id, "operator_id": operator_id, "at": _AT},
    )
    return conversation_id


def _insert_session(conn: Connection, conversation_id: UUID, **columns: str) -> UUID:
    """Name every column head requires and nothing else, so what the caller leaves out is the test.

    `conversation_id` is `NOT NULL` since `0072`, and the operator comes off the conversation rather
    than being passed twice.
    """
    session_id = uuid4()
    extra = "".join(f", {column}" for column in columns)
    placeholders = "".join(f", :{column}" for column in columns)
    conn.execute(
        text(
            "INSERT INTO sessions ("
            "  session_id, operator_id, conversation_id, status, bridge_token_fingerprint,"
            f"  lease_expires_at, created_at, updated_at{extra}"
            ") SELECT"
            "  :session_id, operator_id, conversation_id, 'closed', :fingerprint,"
            f"  :at, :at, :at{placeholders}"
            "  FROM conversation WHERE conversation_id = :conversation_id"
        ),
        {"session_id": session_id, "conversation_id": conversation_id, "fingerprint": session_id.bytes, "at": _AT}
        | columns,
    )
    return session_id


def _surface(conn: Connection, session_id: UUID) -> str | None:
    surface: str | None = conn.execute(
        text("SELECT surface FROM sessions WHERE session_id = :session_id"), {"session_id": session_id}
    ).scalar_one()
    return surface


def test_a_session_inserted_without_a_surface_records_none(db_url: str) -> None:
    """The failure mode this prevents: SQLAlchemy names only mapped columns, so the release that
    unmaps `surface` omits it from every insert and a bare `NOT NULL` would reject the first session
    of that roll. It lands NULL rather than a default, because a defaulted `'spa'` would be a false
    statement about a session a Matrix room opened."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _insert_session(conn, _conversation(conn))
        with engine.connect() as conn:
            assert _surface(conn, session_id) is None
    finally:
        engine.dispose()


def test_a_matrix_session_names_no_room(db_url: str) -> None:
    """`ck_sessions_matrix_room` required a `matrix` session to carry a room id, and the room it
    carried is now the `address` of its conversation's live attachment."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _insert_session(conn, _conversation(conn), surface="matrix")
        with engine.connect() as conn:
            assert _surface(conn, session_id) == "matrix"
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
