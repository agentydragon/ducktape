"""One conversation holds a live address at a time, so a room can be handed to a new one."""

from __future__ import annotations

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
