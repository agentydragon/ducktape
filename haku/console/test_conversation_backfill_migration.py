"""0063 gives every session a conversation, and every served room an attachment to one."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO operators (operator_id, status, created_at, updated_at) "
            "VALUES (:operator_id, 'active', now(), now())"
        ),
        {"operator_id": operator_id},
    )
    return operator_id


def _session(conn: Connection, operator_id: UUID, *, room_id: str | None, minutes: int) -> UUID:
    session_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO sessions ("
            "  session_id, operator_id, surface, room_id, status, bridge_token_fingerprint,"
            "  lease_expires_at, created_at, updated_at"
            ") VALUES ("
            "  :session_id, :operator_id, :surface, :room_id, 'closed', :fingerprint,"
            "  :at, :at, :at"
            ")"
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "surface": "matrix" if room_id is not None else "spa",
            "room_id": room_id,
            "fingerprint": session_id.bytes,
            "at": datetime.datetime(2026, 8, 17, 12, minutes, tzinfo=datetime.UTC),
        },
    )
    return session_id


def test_matrix_sessions_share_a_room_s_conversation_and_every_other_session_gets_its_own(db_url: str) -> None:
    """The grouping is what makes the successive sessions that served a room one thread rather than
    a set nothing can name. Everything else was one conversation already; it just had no id."""
    apply_migrations(db_url, "0062")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            first = _session(conn, operator_id, room_id="!room:example.org", minutes=1)
            second = _session(conn, operator_id, room_id="!room:example.org", minutes=2)
            other_room = _session(conn, operator_id, room_id="!other:example.org", minutes=3)
            spa = _session(conn, operator_id, room_id=None, minutes=4)
            second_spa = _session(conn, operator_id, room_id=None, minutes=5)

        apply_migrations(db_url)

        with engine.connect() as conn:
            conversations = dict(conn.execute(text("SELECT session_id, conversation_id FROM sessions")).tuples().all())
            attachments = conn.execute(
                text("SELECT conversation_id, surface, address, attached_at, detached_at FROM chat_attachment")
            ).all()
            starts = dict(conn.execute(text("SELECT conversation_id, created_at FROM conversation")).tuples().all())
        assert conversations[first] == conversations[second]
        assert (
            len({conversations[first], conversations[other_room], conversations[spa], conversations[second_spa]}) == 4
        )
        assert {(row.conversation_id, row.address) for row in attachments} == {
            (conversations[first], "!room:example.org"),
            (conversations[other_room], "!other:example.org"),
        }
        assert {row.surface for row in attachments} == {"matrix"}
        assert [row.detached_at for row in attachments] == [None, None]
        # The room's conversation starts when its earliest session did, not when the migration ran.
        assert starts[conversations[first]] == datetime.datetime(2026, 8, 17, 12, 1, tzinfo=datetime.UTC)
    finally:
        engine.dispose()


def test_the_previous_image_can_still_create_a_session(db_url: str) -> None:
    """The roll-safety half, and why `sessions.conversation_id` is nullable for one release: the
    previous image's `INSERT` does not name the column, so a `NOT NULL` would reject the first
    session of the roll — which is what `session_frames.partial` hit from the unmapping side."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn, _operator(conn), room_id=None, minutes=6)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT conversation_id FROM sessions WHERE session_id = :s"), {"s": session_id}
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()


def test_one_conversation_holds_an_address_at_a_time_and_detaching_frees_it(db_url: str) -> None:
    """A conversation never ends, so "start this room over" has to be a detach and a re-attach —
    which the partial unique index is what permits."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            first, second = uuid4(), uuid4()
            for conversation_id in (first, second):
                conn.execute(
                    text(
                        "INSERT INTO conversation (conversation_id, operator_id, created_at) "
                        "VALUES (:conversation_id, :operator_id, now())"
                    ),
                    {"conversation_id": conversation_id, "operator_id": operator_id},
                )
            _attach(conn, first, "!room:example.org")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE chat_attachment SET detached_at = now() WHERE conversation_id = :c"), {"c": first}
            )
            _attach(conn, second, "!room:example.org")

        with engine.connect() as conn:
            live = conn.execute(
                text("SELECT conversation_id FROM chat_attachment WHERE detached_at IS NULL")
            ).scalar_one()
        assert live == second
    finally:
        engine.dispose()


def _attach(conn: Connection, conversation_id: UUID, address: str) -> None:
    conn.execute(
        text(
            "INSERT INTO chat_attachment (attachment_id, conversation_id, surface, address, attached_at) "
            "VALUES (:attachment_id, :conversation_id, 'matrix', :address, now())"
        ),
        {"attachment_id": uuid4(), "conversation_id": conversation_id, "address": address},
    )


if __name__ == "__main__":
    pytest_bazel.main()
