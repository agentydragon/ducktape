"""The message provenance migrations: pointers into the raw frame log, and what may lack one."""

from __future__ import annotations

import datetime
import json
from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)


def _session(conn: Connection) -> UUID:
    """One operator serving one session: the two foreign keys a message hangs off."""
    operator_id, session_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
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
    return session_id


def _insert_message(
    conn: Connection, session_id: UUID, *, role: str, first: int | None = None, last: int | None = None
) -> UUID:
    message_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO session_messages (
                message_id, session_id, role, status, content,
                source_first_frame_seq, source_last_frame_seq, tool_uses, created_at, updated_at
            ) VALUES (:message_id, :session_id, :role, 'complete', '',
                      :first, :last, '[]'::jsonb, :n, :n)
            """
        ),
        {"message_id": message_id, "session_id": session_id, "role": role, "first": first, "last": last, "n": _NOW},
    )
    return message_id


@pytest.fixture
def engine(db_url: str) -> Generator[Engine]:
    engine = create_engine(sync_database_url(db_url))
    yield engine
    engine.dispose()


def test_message_provenance_migration_backfills_observed_assistant_frames(db_url: str, engine: Engine) -> None:
    """The historical pointer is rescued only when the old row names its wire message."""
    apply_migrations(db_url, "0044")
    with engine.begin() as conn:
        session_id = _session(conn)
        message_id = uuid4()
        conn.execute(
            text(
                """
                INSERT INTO session_messages (
                    message_id, session_id, role, status, content, agent_message_id,
                    tool_uses, created_at, updated_at
                ) VALUES (:message_id, :session_id, 'assistant', 'complete', '', 'msg_01', '[]'::jsonb, :n, :n)
                """
            ),
            {"message_id": message_id, "session_id": session_id, "n": _NOW},
        )
        for payload in (
            {"type": "assistant", "message": {"id": "msg_01"}},
            {"type": "assistant", "message": {"id": "msg_01"}},
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO session_frames (
                        session_id, direction, kind, payload, partial, created_at, updated_at
                    ) VALUES (:session_id, 'from_agent', 'assistant', CAST(:payload AS jsonb), false, :n, :n)
                    """
                ),
                {"session_id": session_id, "payload": json.dumps(payload), "n": _NOW},
            )

    apply_migrations(db_url)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": message_id},
        ).one() == (1, 2)


def test_unpointed_history_survives_the_constraint(db_url: str, engine: Engine) -> None:
    """`NOT VALID` is the whole reason this ships without archaeology first.

    An assistant row written before #4105 names no wire message, so `0045`'s backfill above cannot
    reach it and it stays unpointed. Migrating past `0046` must leave it exactly where it is
    rather than refusing to apply — the alternative was recovering or dropping history, and
    dropping it would delete the `haku_index` chat corpus.
    """
    apply_migrations(db_url, "0045")
    with engine.begin() as conn:
        message_id = _insert_message(conn, _session(conn), role="assistant")

    apply_migrations(db_url)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": message_id},
        ).one() == (None, None)


def test_a_new_projected_message_must_name_the_frame_it_began_at(db_url: str, engine: Engine) -> None:
    """The gap `0045` left: ordering was checked, having a range at all was not."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_session_messages_projected_source"):
        _insert_message(conn, session_id, role="assistant")

    with engine.begin() as conn:
        pointed = _insert_message(conn, session_id, role="assistant", first=7, last=9)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": pointed},
        ).one() == (7, 9)


def test_an_authored_prompt_needs_no_frames(db_url: str, engine: Engine) -> None:
    """The operator's own row exists before the frame it goes out as, and a prompt no turn ever
    claims never acquires one. Requiring a range here would refuse every prompt at the moment it
    is typed."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)
        message_id = _insert_message(conn, session_id, role="user")

    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT source_first_frame_seq FROM session_messages WHERE message_id = :id"), {"id": message_id}
            ).scalar()
            is None
        )


def test_a_range_cannot_end_where_it_never_began(db_url: str, engine: Engine) -> None:
    """A far end alone is a range in neither kind of row, so this holds for the authored one too."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_session_messages_source_anchored"):
        _insert_message(conn, session_id, role="user", last=4)


def test_pointing_an_authored_prompt_at_its_frame_is_still_allowed(db_url: str, engine: Engine) -> None:
    """`set_message_source_frames` writes both ends onto a row that had neither — the update path
    the constraints have to keep open, since a prompt acquires its frame after it is written."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)
        message_id = _insert_message(conn, session_id, role="user")

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE session_messages SET source_first_frame_seq = 3, source_last_frame_seq = 3 "
                "WHERE message_id = :id"
            ),
            {"id": message_id},
        )

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": message_id},
        ).one() == (3, 3)


if __name__ == "__main__":
    pytest_bazel.main()
