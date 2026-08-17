"""Contracts of the room's own position, and of what it says from it.

The durable half of <../../subscription.py>: this is the only place a position outlives the process
reading it, which is why the restart is asserted here rather than beside the abstraction.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import TurnOutcome
from haku.console.database_schema import MatrixRoomCursor
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM, MATRIX_USER
from haku.console.x.channels.matrix.conversation import MatrixConversationStore
from haku.console.x.channels.matrix.room_subscription import ABORTED_BY_OPERATOR, RoomCursor, RoomNotices
from haku.console.x.session_store import MatrixSession, SessionStore
from haku.console.x.subscription import START, ConversationStream, StreamPosition


class Room:
    """What the room was told, in the order it was told it."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def announce(self, body: str) -> None:
        self.said.append(body)

    async def bound(self) -> str | None:
        return MATRIX_ROOM


@pytest.fixture
def room() -> Room:
    return Room()


@pytest.fixture
def stream(migrated_sessions: async_sessionmaker[AsyncSession]) -> ConversationStream:
    return ConversationStream(migrated_sessions)


@pytest.fixture
def notices(
    migrated_engine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    stream: ConversationStream,
    conversations: MatrixConversationStore,
    notifications,
    room: Room,
) -> RoomNotices:
    return RoomNotices(
        migrated_engine, migrated_sessions, stream, conversations, notifications, room.announce, room.bound
    )


@pytest.fixture
async def served(chat_store: SessionStore, operator_id: UUID, conversations: MatrixConversationStore) -> UUID:
    """A ready session serving the bound room, on the conversation the room is attached to."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    conversation_id = await conversations.conversation_for_room(MATRIX_ROOM, operator_id)
    view, token = await chat_store.create(
        operator_id, MatrixSession(room_id=MATRIX_ROOM), conversation_id=conversation_id
    )
    await chat_store.authenticate_bridge(view.session_id, token)
    return view.session_id


async def abort_a_turn(chat_store: SessionStore, operator_id: UUID, session_id: UUID) -> None:
    await chat_store.enqueue_prompt(operator_id, session_id, "do the thing")
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ABORTED)


async def stored_position(sessions: async_sessionmaker[AsyncSession]) -> StreamPosition | None:
    async with sessions() as db:
        reached = await db.scalar(select(MatrixRoomCursor.event_seq).where(MatrixRoomCursor.room_id == MATRIX_ROOM))
        return None if reached is None else StreamPosition(event_seq=reached)


async def test_a_room_that_has_never_read_takes_the_head_and_says_nothing(
    chat_store, operator_id, served, notices, room, stream, migrated_sessions
) -> None:
    """A room the console has been servicing since before it kept a position already shows what was
    said in it, so a first pass must not replay the conversation into it."""
    await abort_a_turn(chat_store, operator_id, served)

    assert await notices.reconcile_once() is False
    assert room.said == []
    assert await stored_position(migrated_sessions) == await stream.head(await chat_store.conversation_of(served))


async def test_an_abort_recorded_after_the_room_started_reading_becomes_a_notice(
    chat_store, operator_id, served, notices, room
) -> None:
    """An abort recorded while the room was reading becomes a notice."""
    await notices.reconcile_once()

    await abort_a_turn(chat_store, operator_id, served)
    await notices.reconcile_once()

    assert room.said == [ABORTED_BY_OPERATOR]


async def test_what_the_room_has_been_told_is_not_told_again(chat_store, operator_id, served, notices, room) -> None:
    await notices.reconcile_once()
    await abort_a_turn(chat_store, operator_id, served)

    await notices.reconcile_once()
    await notices.reconcile_once()

    assert room.said == [ABORTED_BY_OPERATOR]


async def test_a_restarted_reader_resumes_from_the_position_it_kept(
    chat_store,
    operator_id,
    served,
    notices,
    room,
    migrated_engine,
    migrated_sessions,
    stream,
    conversations,
    notifications,
) -> None:
    """The whole reason this position is durable: the room's copy outlives the process that wrote
    into it, so a replica that goes away mid-conversation must not re-say what its predecessor did
    — nor skip what was recorded while nobody was reading."""
    await notices.reconcile_once()
    await abort_a_turn(chat_store, operator_id, served)
    await notices.reconcile_once()

    successor_room = Room()
    successor = RoomNotices(
        migrated_engine,
        migrated_sessions,
        stream,
        conversations,
        notifications,
        successor_room.announce,
        successor_room.bound,
    )
    await successor.reconcile_once()
    assert successor_room.said == []

    await abort_a_turn(chat_store, operator_id, served)
    await successor.reconcile_once()
    assert successor_room.said == [ABORTED_BY_OPERATOR]


async def test_a_room_with_no_conversation_behind_it_is_not_behind_on_anything(
    conversations, notices, room, migrated_sessions
) -> None:
    """A room bound by an invite the supervisor has not reached yet: nothing recorded, nothing
    owed, and no position taken — the next pass, once it has a conversation, still starts at its
    head rather than at zero."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)

    assert await notices.reconcile_once() is False
    assert (room.said, await stored_position(migrated_sessions)) == ([], None)


async def test_an_unread_room_has_no_position_at_all(migrated_sessions) -> None:
    """Absent is "never read", not "at the start" — the distinction the seeding above turns on."""
    assert await RoomCursor(migrated_sessions, MATRIX_ROOM).position() is None


async def test_keeping_a_position_twice_moves_it_rather_than_failing(migrated_sessions) -> None:
    """Whichever replica holds the notices lock writes this row, and leadership changes hands."""
    cursor = RoomCursor(migrated_sessions, MATRIX_ROOM)

    await cursor.keep(START)
    await cursor.keep(StreamPosition(event_seq=17))

    assert await cursor.position() == StreamPosition(event_seq=17)


if __name__ == "__main__":
    pytest_bazel.main()
