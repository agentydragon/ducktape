"""`ck_conversation_event_provenance_frames`: what a row on each arm of the union must carry.

Stated in Postgres rather than in the writer because the reader that matters is not the writer: a
row on the `frame_range` arm names the frames its content was read off, and tracing them back needs
the range, the turn, the session and the item — so a row on that arm missing any of those is one
nothing can check, and a range with one end is neither a range nor the absence of one.
"""

from __future__ import annotations

import datetime
from collections.abc import Generator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 19, tzinfo=datetime.UTC)


@dataclass(frozen=True)
class _Conversation:
    """The four ids an event may name, all of them written."""

    conversation_id: UUID
    session_id: UUID
    turn_id: UUID
    item_id: UUID


def _conversation(conn: Connection) -> _Conversation:
    operator_id = uuid4()
    written = _Conversation(conversation_id=uuid4(), session_id=uuid4(), turn_id=uuid4(), item_id=uuid4())
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, harness_kind, created_at) "
            "VALUES (:id, :o, 'claude_code', :n)"
        ),
        {"id": written.conversation_id, "o": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, bridge_token_fingerprint,
                bridge_connected_at, lease_expires_at, created_at, updated_at
            ) VALUES (:s, :o, :c, :fp, :n, :n, :n, :n)
            """
        ),
        {"s": written.session_id, "o": operator_id, "c": written.conversation_id, "fp": b"fp", "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_turn (turn_id, conversation_id, session_id, first_seq, started_at)
            VALUES (:t, :c, :s, 1, :n)
            """
        ),
        {"t": written.turn_id, "c": written.conversation_id, "s": written.session_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_item (
                item_id, conversation_id, session_id, turn_id, item_type, status,
                opened_seq, text, created_at, updated_at
            ) VALUES (:i, :c, :s, :t, 'message', 'open', 1, '', :n, :n)
            """
        ),
        {"i": written.item_id, "c": written.conversation_id, "s": written.session_id, "t": written.turn_id, "n": _NOW},
    )
    return written


_EVENT = text(
    """
    INSERT INTO conversation_event (conversation_id, event_seq, session_id, turn_id, item_id, kind,
                                    provenance, source_first_frame_seq, source_last_frame_seq,
                                    body, created_at)
    VALUES (:conversation_id, :event_seq, :session_id, :turn_id, :item_id, 'item_segment',
            :provenance, :first, :last, '{}'::jsonb, :n)
    """
)


@pytest.fixture
def engine(db_url: str) -> Generator[Engine]:
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    yield engine
    engine.dispose()


def _write(
    conn: Connection,
    conversation: _Conversation,
    *,
    event_seq: int,
    provenance: str,
    first: int | None = None,
    last: int | None = None,
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> None:
    conn.execute(
        _EVENT,
        {
            "conversation_id": conversation.conversation_id,
            "event_seq": event_seq,
            "session_id": session_id,
            "turn_id": turn_id,
            "item_id": conversation.item_id,
            "provenance": provenance,
            "first": first,
            "last": last,
            "n": _NOW,
        },
    )


def test_the_two_writer_shapes_are_accepted(engine: Engine) -> None:
    """A folded segment naming its frames and its turn, and an authored one naming neither: the two
    shapes anything writes, so the constraint has to admit both."""
    with engine.begin() as conn:
        conversation = _conversation(conn)
        _write(
            conn,
            conversation,
            event_seq=1,
            provenance="frame_range",
            first=7,
            last=9,
            session_id=conversation.session_id,
            turn_id=conversation.turn_id,
        )
        _write(conn, conversation, event_seq=2, provenance="authored")

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT provenance, source_first_frame_seq FROM conversation_event ORDER BY event_seq")
        ).all()
        # A Row equals the tuple of its values at runtime, but is a distinct type to mypy.
        assert [tuple(row) for row in rows] == [("frame_range", 7), ("authored", None)]


@pytest.mark.parametrize(
    ("provenance", "first", "last", "names_turn"),
    [
        # A far end with no near end is neither a range nor the absence of one.
        pytest.param("frame_range", None, 4, True, id="half-range"),
        # Frames belong to exactly one arm, either way round.
        pytest.param("authored", 7, 9, True, id="authored-with-frames"),
        pytest.param("frame_range", None, None, True, id="folded-without-frames"),
        # A range is read back by re-folding the turn's frames, so the row has to name that turn.
        pytest.param("frame_range", 7, 9, False, id="folded-without-turn"),
    ],
)
def test_shapes_the_union_refuses(
    engine: Engine, provenance: str, first: int | None, last: int | None, names_turn: bool
) -> None:
    with engine.begin() as conn:
        conversation = _conversation(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_conversation_event_provenance_frames"):
        _write(
            conn,
            conversation,
            event_seq=1,
            provenance=provenance,
            first=first,
            last=last,
            session_id=conversation.session_id,
            turn_id=conversation.turn_id if names_turn else None,
        )


def test_a_backwards_range_is_refused(engine: Engine) -> None:
    with engine.begin() as conn:
        conversation = _conversation(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_conversation_event_provenance_frames"):
        _write(
            conn,
            conversation,
            event_seq=1,
            provenance="frame_range",
            first=9,
            last=7,
            session_id=conversation.session_id,
            turn_id=conversation.turn_id,
        )


if __name__ == "__main__":
    pytest_bazel.main()
