"""What the room's outbox promises: a reply is said once, and a refused one is said later.

Against a real Postgres, because the promise is a property of rows and indexes — the claim that
charges an attempt, the partial unique index that stops one message being queued twice — and a
fake store would be asserting the fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import SPA_ORIGIN, ItemStatus, ItemType
from haku.console.database_schema import ConversationItem, MatrixOutbox
from haku.console.x.channels.matrix.client import MatrixError
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM
from haku.console.x.channels.matrix.conversation import MatrixConversationStore
from haku.console.x.channels.matrix.outbox import MAX_SEND_ATTEMPTS, PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.x.channels.matrix.pacer import RoomPacer
from haku.console.x.conversation_events import FrameRange, ItemSegment, MessageCompleted, MessageStarted, OpenRef
from haku.console.x.session_store import BridgeAuthentication, SessionStore


@pytest.fixture
def outbox(migrated_sessions: async_sessionmaker[AsyncSession]) -> RoomOutbox:
    return RoomOutbox(migrated_sessions)


@pytest.fixture
async def attachment_id(conversations: MatrixConversationStore, operator_id: UUID) -> UUID:
    """The room's attachment, which is what a sent reply is recorded against."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    attachment_id = await conversations.attachment(MATRIX_ROOM)
    assert attachment_id is not None
    return attachment_id


@pytest.fixture
async def session_id(chat_store: SessionStore, conversations: MatrixConversationStore, operator_id: UUID) -> UUID:
    """A live Matrix session for `MATRIX_ROOM`, since an outbox row is one of its children.

    Started on the conversation the room is attached to, the way the supervisor starts one.
    """
    view, token = await chat_store.create(
        operator_id, conversation_id=(await conversations.bind_room(MATRIX_ROOM, operator_id)).conversation_id
    )
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


@pytest.fixture
async def turn_id(chat_store: SessionStore, operator_id: UUID, session_id: UUID) -> UUID:
    """The exchange these replies are produced in."""
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    return turn.turn_id


class _Homeserver:
    """What the drain sends through, refusing whatever it has been told to refuse."""

    def __init__(self, *, refuses: set[str] | None = None) -> None:
        self.posted: list[str] = []
        self.transactions: list[str] = []
        self._refuses = refuses or set()

    async def post(self, reply: PendingReply) -> str:
        self.transactions.append(reply.transaction_id())
        if reply.body in self._refuses:
            raise MatrixError("429: slow down")
        self.posted.append(reply.body)
        return f"$event-{len(self.posted)}"

    def accepts_everything(self) -> None:
        self._refuses = set()


def _unpaced(engine: AsyncEngine, outbox: RoomOutbox, homeserver: _Homeserver) -> tuple[RoomPacer, RoomOutboxDrain]:
    """A drain over a pacer with effectively no rate, so a test waits on outcomes not on tokens.

    The rate itself is `pacer`'s and is asserted there.
    """
    pacer = RoomPacer(sends_per_second=1e6, burst=100)
    return pacer, RoomOutboxDrain(engine, outbox, pacer, homeserver.post, _room)


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[MatrixOutbox]:
    async with sessions() as db:
        return list(await db.scalars(select(MatrixOutbox).order_by(MatrixOutbox.created_at)))


async def _said(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[UUID]:
    """The completed message items of a session, oldest first — what a subscriber sees complete."""
    async with sessions() as db:
        return list(
            await db.scalars(
                select(ConversationItem.item_id)
                .where(
                    ConversationItem.session_id == session_id,
                    ConversationItem.item_type == ItemType.MESSAGE,
                    ConversationItem.status == ItemStatus.COMPLETE,
                )
                .order_by(ConversationItem.opened_seq)
            )
        )


async def _enqueue(
    chat_store: SessionStore,
    sessions: async_sessionmaker[AsyncSession],
    outbox: RoomOutbox,
    attachment_id: UUID,
    session_id: UUID,
    turn_id: UUID,
    *bodies: str,
) -> None:
    """Produce *bodies* the way a turn does — one completed message item each — and queue them the
    way the room's subscriber does when it reads those completions off the log."""
    for frame_seq, body in enumerate(bodies, start=1):
        where = FrameRange(frame_seq, frame_seq)
        await chat_store.apply_frame(
            session_id,
            turn_id,
            frame_seq,
            [
                MessageStarted(provenance=where),
                ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=body, provenance=where),
                MessageCompleted(backend_item_id=None, provenance=where),
            ],
        )
    for item_id in await _said(sessions, session_id):
        await outbox.enqueue(attachment_id, item_id)


async def test_a_reply_is_said_once_and_then_never_again(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id
) -> None:
    """The half of `exactly once` a redrive could break: a row the homeserver accepted is `sent_at`
    and never claimed again."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()
        assert not await drain.drain_once(), "a sent row was claimed a second time"

    assert homeserver.posted == ["the answer"]
    [row] = await _rows(migrated_sessions)
    assert (row.sent_at is not None, row.attempts, row.last_error) == (True, 1, None)


async def test_replies_are_said_in_the_order_they_were_produced(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id
) -> None:
    """A turn that narrates, works and reports back is three rows, and the room reads top to
    bottom: out of order they describe a different turn."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(
        chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "looking now", "found it", "fixed"
    )

    async with pacer.run():
        while await drain.drain_once():
            pass

    assert homeserver.posted == ["looking now", "found it", "fixed"]


async def test_a_refused_send_leaves_the_row_for_the_next_attempt(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id
) -> None:
    """The drop this table exists for (<../../../debug/message_drops.md> E1): the failure is the
    row's — unsent, one attempt spent, the homeserver's own words kept, and claimable again once
    its backoff passes.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()

    assert homeserver.posted == []
    [row] = await _rows(migrated_sessions)
    assert row.sent_at is None, "a send that raised must not count as delivered"
    assert (row.attempts, row.last_error) == (1, "429: slow down")


async def test_a_reply_the_room_refused_is_said_once_the_homeserver_relents(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id
) -> None:
    """A produced reply is retried rather than lost. The wait is skipped by hand: what is under
    test is that the row comes back at all, not how long it waits first."""
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()
        homeserver.accepts_everything()
        await _due_now(migrated_sessions)
        assert await drain.drain_once()
        await pacer.flush()

    assert homeserver.posted == ["the answer"]
    assert len(set(homeserver.transactions)) == 1, "a redrive must reuse its transaction id, or it posts twice"


async def test_a_refused_reply_holds_up_the_one_behind_it(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id
) -> None:
    """Order survives a retry, which is the half of this that a due-rows-only query would lose.

    A reply waiting out its backoff is not overtaken by the next one: the room is read top to
    bottom, so an answer arriving before the answer it follows describes a different turn.
    """
    homeserver = _Homeserver(refuses={"found it"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(
        chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "found it", "and fixed it"
    )

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()
        assert not await drain.drain_once(), "the reply behind a failed one was sent ahead of it"
        homeserver.accepts_everything()
        await _due_now(migrated_sessions)
        while await drain.drain_once():
            await pacer.flush()

    assert homeserver.posted == ["found it", "and fixed it"]


async def test_a_reply_out_of_attempts_is_left_alone_rather_than_retried_forever(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, attachment_id, caplog
) -> None:
    """A room that has refused the same message eight times is not going to take the ninth.

    So this is the one row the ordered queue steps over — kept, unsent, with the reason on it —
    while the reply behind it, which the head-of-line rule would otherwise strand forever, goes out.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(
        chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer", "and the next one"
    )

    async with pacer.run():
        for _ in range(MAX_SEND_ATTEMPTS):
            assert await drain.drain_once()
            await pacer.flush()
            await _due_now(migrated_sessions)
        assert await drain.drain_once()
        await pacer.flush()
        assert not await drain.drain_once()

    assert homeserver.posted == ["and the next one"]
    refused, behind = await _rows(migrated_sessions)
    assert (refused.sent_at, refused.attempts) == (None, MAX_SEND_ATTEMPTS)
    assert behind.sent_at is not None, "a dead row must not wedge the queue behind it"
    assert "giving up on outbox row" in caplog.text


async def test_one_item_is_queued_once_however_often_a_subscriber_sees_it_complete(
    chat_store, migrated_sessions, outbox, session_id, turn_id, attachment_id
) -> None:
    """A subscriber that crashed between sending and keeping its position sees the same completion
    again, and so does a runner replaying its rollout into a replacement replica. Without the
    partial unique index that would be a second row for one item, and the room would read the answer
    twice."""
    await _enqueue(chat_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")
    item_id = one(await _said(migrated_sessions, session_id))

    for _ in range(3):
        await outbox.enqueue(attachment_id, item_id)

    [row] = await _rows(migrated_sessions)
    assert (row.subject, row.body) == (item_id.hex, "the answer")


async def test_an_item_that_said_nothing_is_not_a_reply(
    chat_store, migrated_sessions, outbox, session_id, turn_id, attachment_id
) -> None:
    """A turn that only ran tools said nothing, and an empty room event would be the console
    reporting that as an answer."""
    where = FrameRange(1, 1)
    await chat_store.apply_frame(
        session_id,
        turn_id,
        1,
        [MessageStarted(provenance=where), MessageCompleted(backend_item_id=None, provenance=where)],
    )

    assert not await outbox.enqueue(attachment_id, one(await _said(migrated_sessions, session_id)))
    assert await _rows(migrated_sessions) == []


async def test_nothing_is_claimed_before_a_room_is_bound(migrated_engine, outbox) -> None:
    """A console the operator has not invited anywhere has nowhere to drain to, and asking the
    database on every poll for a room that does not exist is the one case worth short-circuiting.
    """

    async def unbound() -> str | None:
        return None

    drain = RoomOutboxDrain(migrated_engine, outbox, RoomPacer(), _never_posted, unbound)

    assert not await drain.drain_once()


async def _room() -> str | None:
    return MATRIX_ROOM


async def _never_posted(reply: PendingReply) -> str:
    raise AssertionError(f"nothing should have been sent, but {reply.body!r} was")


async def _due_now(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Bring every unsent row's retry forward, so a test waits on outcomes rather than on clocks."""
    async with sessions() as db, db.begin():
        for row in await db.scalars(select(MatrixOutbox).where(MatrixOutbox.sent_at.is_(None))):
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


if __name__ == "__main__":
    pytest_bazel.main()
