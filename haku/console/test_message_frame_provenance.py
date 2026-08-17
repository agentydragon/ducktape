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


def _conversation(conn: Connection, operator_id: UUID) -> UUID:
    conversation_id = uuid4()
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :operator_id, :n)"),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    return conversation_id


def _session(conn: Connection, *, threaded: bool = True) -> UUID:
    """One operator serving one session: the two foreign keys a message hangs off.

    *threaded* is False for a revision older than `0064`, which is where `conversation` and the
    column pointing at it begin; from `0072` on, naming it is the only way to insert a session.
    """
    operator_id, session_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    columns: dict[str, object] = {
        "session_id": session_id,
        "operator_id": operator_id,
        "surface": "spa",
        "status": "ready",
        "bridge_token_fingerprint": b"fingerprint",
    }
    if threaded:
        columns["conversation_id"] = _conversation(conn, operator_id)
    named = ", ".join(columns)
    conn.execute(
        text(
            f"INSERT INTO sessions ({named}, lease_expires_at, created_at, updated_at) "
            f"VALUES ({', '.join(f':{name}' for name in columns)}, :n, :n, :n)"
        ),
        columns | {"n": _NOW},
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
                source_first_frame_seq, source_last_frame_seq, created_at, updated_at
            ) VALUES (:message_id, :session_id, :role, 'complete', '', :first, :last, :n, :n)
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
        session_id = _session(conn, threaded=False)
        message_id = uuid4()
        conn.execute(
            text(
                """
                INSERT INTO session_messages (
                    message_id, session_id, role, status, content, agent_message_id,
                    created_at, updated_at
                ) VALUES (:message_id, :session_id, 'assistant', 'complete', '', 'msg_01', :n, :n)
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
    """`NOT VALID` is the whole reason `0046` shipped without archaeology first.

    An assistant row written before #4105 names no wire message, so `0045`'s backfill above cannot
    reach it and it stays unpointed. Applying `0046` had to leave it exactly where it is rather
    than refusing — the alternative was recovering or dropping history, and dropping it would have
    deleted the `haku_index` chat corpus. It was dropped in the end (`0058`, and the purge that
    preceded it), which is why this stops at the revision it is about.
    """
    apply_migrations(db_url, "0045")
    with engine.begin() as conn:
        message_id = _insert_message(conn, _session(conn, threaded=False), role="assistant")

    apply_migrations(db_url, "0046")

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": message_id},
        ).one() == (None, None)


def test_a_range_cannot_end_where_it_never_began(db_url: str, engine: Engine) -> None:
    """The shape `0045` left writable that is nonsense under either arm of the union: a far end
    with no near end is neither a range nor the absence of one.

    On the prompt, since an assistant row is refused for the stricter reason below.
    """
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_session_messages_source_anchored"):
        _insert_message(conn, session_id, role="user", last=4)


def test_an_assistant_row_must_say_which_frame_opened_it(db_url: str, engine: Engine) -> None:
    """`begin_assistant` names that frame at insert, so an unpointed answer is one nothing can
    appeal to the log. Refusable only because the purge deleted the rows that were in that shape
    (<debug/2026_08_16_legacy_purge.md>)."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        session_id = _session(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_session_messages_assistant_pointed"):
        _insert_message(conn, session_id, role="assistant")


@pytest.mark.parametrize(
    ("role", "first", "last"),
    [
        # The operator's own prompt at the moment it is typed: written before the frame it goes
        # out as exists, and never pointed at all if no turn claims it.
        pytest.param("user", None, None, id="unclaimed-prompt"),
        # `begin_assistant` at insert: the near end alone, before any delta has widened it.
        pytest.param("assistant", 7, None, id="opened"),
        pytest.param("assistant", 7, 9, id="completed"),
    ],
)
def test_writer_shapes_are_accepted(
    db_url: str, engine: Engine, role: str, first: int | None, last: int | None
) -> None:
    apply_migrations(db_url)
    with engine.begin() as conn:
        message_id = _insert_message(conn, _session(conn), role=role, first=first, last=last)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"),
            {"id": message_id},
        ).one() == (first, last)


def test_pointing_an_authored_prompt_at_its_frame_is_still_allowed(db_url: str, engine: Engine) -> None:
    """`set_message_source_frames` writes both ends onto a row that had neither — the update path
    the constraint has to keep open, since a prompt acquires its frame after it is written. A
    `NOT VALID` check is enforced on `UPDATE` as well as `INSERT`, so this is not free."""
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
