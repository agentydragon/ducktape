"""Contracts of the room's own position, and of what it says from it.

The durable half of <../../subscription.py>: this is the only place a position outlives the process
reading it, which is why the restart is asserted here rather than beside the abstraction.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import LeaseExpiryReason, MatrixOrigin, PromptRejection, SpaOrigin, TurnOutcome
from haku.console.database_schema import MatrixRoomCursor
from haku.console.x import session_events
from haku.console.x.channels.matrix.client import RoomEventKind
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM
from haku.console.x.channels.matrix.conversation import MatrixConversationStore
from haku.console.x.channels.matrix.room_subscription import (
    ABORTED_BY_OPERATOR,
    RELAYED_PROMPT,
    RoomCursor,
    RoomNotices,
)
from haku.console.x.session_store import SessionStore
from haku.console.x.subscription import START, ConversationStream, StreamPosition


class Room:
    """What the room was told, in the order it was told it."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.kinds: list[RoomEventKind] = []

    async def announce(self, body: str, kind: RoomEventKind) -> None:
        self.said.append(body)
        self.kinds.append(kind)

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
    conversation_id = (await conversations.bind_room(MATRIX_ROOM, operator_id)).conversation_id
    view, token = await chat_store.create(operator_id, conversation_id=conversation_id)
    await chat_store.authenticate_bridge(view.session_id, token)
    return view.session_id


async def abort_a_turn(chat_store: SessionStore, operator_id: UUID, session_id: UUID) -> None:
    await chat_store.enqueue_prompt(
        operator_id, session_id, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
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
    notices, room, migrated_sessions
) -> None:
    """A room this console holds no conversation for: nothing recorded, nothing owed, and no
    position taken — the next pass, once it has one, still starts at its head rather than at
    zero."""
    assert await notices.reconcile_once() is False
    assert (room.said, await stored_position(migrated_sessions)) == ([], None)


async def author(
    sessions: async_sessionmaker[AsyncSession], session_id: UUID, body: session_events.AuthoredBody
) -> None:
    """Write one of the console's own facts about *session_id*, as its own writer would."""
    async with sessions() as db, db.begin():
        db.add(session_events.authored(body, session_id=session_id, now=datetime.datetime.now(datetime.UTC)))


async def test_a_refused_prompt_is_said_from_its_row_rather_than_by_ingress(
    chat_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """The row ingress wrote with the watermark is the notice: the sync loop records and this says
    it, so one refusal is not both recorded and pushed."""
    await notices.reconcile_once()
    await author(
        migrated_sessions,
        served,
        session_events.PromptRejectedBody(reason=PromptRejection.TURN_IN_FLIGHT, text="and this"),
    )

    await notices.reconcile_once()

    assert room.said == ["not delivered — Haku is still working on the previous message; send it again"]
    assert room.kinds == [RoomEventKind.REJECTED]


async def test_something_haku_cannot_read_is_said_from_its_row(
    chat_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    await notices.reconcile_once()
    await author(migrated_sessions, served, session_events.UnreadableInputBody(media_type="m.image"))

    await notices.reconcile_once()

    assert "m.image" in room.said[0]
    assert room.kinds == [RoomEventKind.UNREADABLE]


async def test_the_room_is_told_when_its_session_changes_hands_or_ends(
    chat_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """Both are caused by a session and are conversation facts: what the operator needs is to know
    why the room went quiet, which is the same question either way."""
    await notices.reconcile_once()
    await author(migrated_sessions, served, session_events.SessionAdoptedBody(previous_holder="pod-a", holder="pod-b"))
    await author(
        migrated_sessions,
        served,
        session_events.LeaseExpiredBody(reason=LeaseExpiryReason.HOLDER_GONE, last_holder="pod-b"),
    )

    await notices.reconcile_once()

    assert room.said == [
        "another console replica (pod-b) took this session over",
        "the session ended — the console replica serving it went away",
    ]


async def test_a_prompt_sent_from_another_surface_is_posted_into_the_room(
    chat_store, operator_id, served, notices, room
) -> None:
    """A prompt is a conversation fact, so every attached surface shows it — including the one it
    did not arrive through."""
    await notices.reconcile_once()
    await chat_store.enqueue_prompt(operator_id, served, "from the console", SpaOrigin())

    await notices.reconcile_once()

    assert room.said == [RELAYED_PROMPT + "from the console"]
    assert room.kinds == [RoomEventKind.NARRATION]


async def test_a_prompt_typed_into_this_room_is_not_posted_back_into_it(
    chat_store, operator_id, served, notices, room
) -> None:
    """`PromptBody.origin`'s reader: the message is already in the timeline above, so posting it
    again would show the operator their own sentence twice."""
    await notices.reconcile_once()
    await chat_store.enqueue_prompt(
        operator_id, served, "typed here", MatrixOrigin(address=MATRIX_ROOM, refs=("$here",))
    )

    await notices.reconcile_once()

    assert room.said == []


async def test_a_prompt_from_a_sibling_room_is_posted_because_the_address_differs(
    chat_store, operator_id, served, notices, room
) -> None:
    """An equality test against the address, not a look inside one: a bare event id could not tell
    a sibling room's copy from this room's."""
    await notices.reconcile_once()
    await chat_store.enqueue_prompt(
        operator_id, served, "next door", MatrixOrigin(address="!other:allegedly.works", refs=("$there",))
    )

    await notices.reconcile_once()

    assert room.said == [RELAYED_PROMPT + "next door"]


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
