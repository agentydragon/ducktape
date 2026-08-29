"""What the room's outbox promises: a reply is said once, and a refused one is said later.

Against a real Postgres, because the promise is a property of rows and indexes — the claim that
charges an attempt, the partial unique index that stops one message being queued twice — and a
fake store would be asserting the fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.matrix.client import Error
from haku.console.channels.matrix.conftest import MATRIX_ROOM, MATRIX_TEST_HARNESS_KIND
from haku.console.channels.matrix.conversation import ConversationStore, RoomAttachment
from haku.console.channels.matrix.outbox import MAX_SEND_ATTEMPTS, PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.channels.matrix.outbox_wake import OutboxWakes
from haku.console.channels.matrix.pacer import RoomPacer
from haku.console.conversation.conversation_event import FrameRange
from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.database_schema import ConversationItem, MatrixOutbox
from haku.console.session.store import RunnerConnectionAuthentication, Store
from haku.console.x.conversation_events import ItemSegment, MessageCompleted, MessageStarted, OpenRef


@pytest.fixture
def outbox(migrated_sessions: async_sessionmaker[AsyncSession]) -> RoomOutbox:
    return RoomOutbox(migrated_sessions)


@pytest.fixture
async def binding(conversations: ConversationStore, operator_id: UUID) -> RoomAttachment:
    """The room's live binding, whose attachment is what a sent reply is recorded against."""
    return await conversations.bind_room(MATRIX_ROOM, operator_id, harness_kind=MATRIX_TEST_HARNESS_KIND)


@pytest.fixture
def attachment_id(binding: RoomAttachment) -> UUID:
    return binding.attachment_id


@pytest.fixture
async def session_id(session_store: Store, binding: RoomAttachment, operator_id: UUID) -> UUID:
    """A live Matrix session for `MATRIX_ROOM`, since an outbox row is one of its children.

    Started on the conversation the room is attached to, the way the supervisor starts one.
    """
    view, token = await session_store.create(operator_id, conversation_id=binding.conversation_id)
    assert (
        await session_store.authenticate_runner_connection(view.session_id, token)
        == RunnerConnectionAuthentication.ACCEPTED
    )
    return view.session_id


@pytest.fixture
async def turn_id(session_store: Store, operator_id: UUID, session_id: UUID) -> UUID:
    """The exchange these replies are produced in."""
    await session_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(session_id)
    assert turn is not None
    return turn.turn_id


class _Homeserver:
    """What the drain sends through, refusing whatever it has been told to refuse."""

    def __init__(self, *, refuses: set[str] | None = None) -> None:
        self.posted: list[str] = []
        self.transactions: list[str] = []
        self.said = asyncio.Event()
        """Set on each accepted post, for a test that waits on the running drain's outcome."""
        self._refuses = refuses or set()

    async def post(self, reply: PendingReply) -> str:
        self.transactions.append(reply.transaction_id())
        if reply.body in self._refuses:
            raise Error("429: slow down")
        self.posted.append(reply.body)
        self.said.set()
        return f"$event-{len(self.posted)}"

    def accepts_everything(self) -> None:
        self._refuses = set()


def _unpaced(binding: RoomAttachment, outbox: RoomOutbox, homeserver: _Homeserver) -> tuple[RoomPacer, RoomOutboxDrain]:
    """A drain over a pacer with effectively no rate, so a test waits on outcomes not on tokens.

    The rate itself is `pacer`'s and is asserted there. The wake wire is registered in `run()`,
    which these `drain_once`-driving tests never enter; `test_an_enqueue_wakes_the_running_drain`
    does, with a real listener.
    """
    pacer = RoomPacer(sends_per_second=1e6, burst=100)
    return pacer, RoomOutboxDrain(outbox, pacer, homeserver.post, binding, cast(Any, None))


@pytest.fixture
async def outbox_wakes(migrated_db_url: str) -> AsyncIterator[OutboxWakes]:
    """A real listener on the outbox wire — the plumbing is the thing under test."""
    wire = OutboxWakes(migrated_db_url)
    await wire.start()
    try:
        yield wire
    finally:
        await wire.aclose()


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
    session_store: Store,
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
        await session_store.apply_frame(
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
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """The half of `exactly once` a redrive could break: a row the homeserver accepted is `sent_at`
    and never claimed again."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()
        assert not await drain.drain_once(), "a sent row was claimed a second time"

    assert homeserver.posted == ["the answer"]
    [row] = await _rows(migrated_sessions)
    assert (row.sent_at is not None, row.attempts, row.last_error) == (True, 1, None)


async def test_replies_are_said_in_the_order_they_were_produced(
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """A turn that narrates, works and reports back is three rows, and the room reads top to
    bottom: out of order they describe a different turn."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(
        session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "looking now", "found it", "fixed"
    )

    async with pacer.run():
        while await drain.drain_once():
            pass

    assert homeserver.posted == ["looking now", "found it", "fixed"]


async def test_a_refused_send_leaves_the_row_for_the_next_attempt(
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """A failed send leaves the row unsent and claimable again once its backoff passes.

    The homeserver's own words remain recorded, while the failed row spends one attempt.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()

    assert homeserver.posted == []
    [row] = await _rows(migrated_sessions)
    assert row.sent_at is None, "a send that raised must not count as delivered"
    assert (row.attempts, row.last_error) == (1, "429: slow down")


async def test_a_reply_the_room_refused_is_said_once_the_homeserver_relents(
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """A produced reply is retried rather than lost. The wait is skipped by hand: what is under
    test is that the row comes back at all, not how long it waits first."""
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")

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
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """Order survives a retry, which is the half of this that a due-rows-only query would lose.

    A reply waiting out its backoff is not overtaken by the next one: the room is read top to
    bottom, so an answer arriving before the answer it follows describes a different turn.
    """
    homeserver = _Homeserver(refuses={"found it"})
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(
        session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "found it", "and fixed it"
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
    session_store, migrated_sessions, outbox, binding, session_id, turn_id, attachment_id, caplog
) -> None:
    """A room that has refused the same message eight times is not going to take the ninth.

    So this is the one row the ordered queue steps over — kept, unsent, with the reason on it —
    while the reply behind it, which the head-of-line rule would otherwise strand forever, goes out.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(binding, outbox, homeserver)
    await _enqueue(
        session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer", "and the next one"
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
    session_store, migrated_sessions, outbox, session_id, turn_id, attachment_id
) -> None:
    """A subscriber that crashed between sending and keeping its position sees the same completion
    again, and so does a runner replaying its rollout into a replacement replica. Without the
    partial unique index that would be a second row for one item, and the room would read the answer
    twice."""
    await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")
    item_id = one(await _said(migrated_sessions, session_id))

    for _ in range(3):
        await outbox.enqueue(attachment_id, item_id)

    [row] = await _rows(migrated_sessions)
    assert (row.subject, row.body) == (item_id.hex, "the answer")


async def test_an_item_that_said_nothing_is_not_a_reply(
    session_store, migrated_sessions, outbox, session_id, turn_id, attachment_id
) -> None:
    """A turn that only ran tools said nothing, and an empty room event would be the console
    reporting that as an answer."""
    where = FrameRange(1, 1)
    await session_store.apply_frame(
        session_id,
        turn_id,
        1,
        [MessageStarted(provenance=where), MessageCompleted(backend_item_id=None, provenance=where)],
    )

    assert not await outbox.enqueue(attachment_id, one(await _said(migrated_sessions, session_id)))
    assert await _rows(migrated_sessions) == []


async def test_an_enqueued_reply_wakes_the_outbox_wire(
    session_store, migrated_sessions, outbox_wakes, outbox, session_id, turn_id, attachment_id
) -> None:
    """The wake rides the enqueue's own transaction, so it cannot precede the row it announces.

    Nothing earlier can wake the drain: the conversation wake that made the enqueueing subscriber
    read had already fired before the row existed, possibly heard on another replica.
    """
    woken = asyncio.Event()

    with outbox_wakes.watch(woken.set):
        woken.clear()  # the registration-time state does not count; only the enqueue's wake does
        await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")
        async with asyncio.timeout(10):
            await woken.wait()


async def test_a_duplicate_enqueue_wakes_nobody(
    session_store, migrated_sessions, outbox_wakes, outbox, session_id, turn_id, attachment_id
) -> None:
    """The enqueue that inserted the row already woke the drain; a conflict announces nothing new."""
    await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")
    item_id = one(await _said(migrated_sessions, session_id))
    woken = asyncio.Event()

    with outbox_wakes.watch(woken.set):
        woken.clear()
        assert not await outbox.enqueue(attachment_id, item_id)
        # Delivery is asynchronous, so an absence can only be established by waiting one out.
        await asyncio.sleep(1)

    assert not woken.is_set()


async def test_a_reconnected_wire_wakes_its_registrations(migrated_sessions, outbox_wakes) -> None:
    """Notifications committed during a listener gap are gone, so a re-established LISTEN wakes
    everyone: "look at the table" is the only wake this wire has, and it is always correct."""
    woken = asyncio.Event()

    with outbox_wakes.watch(woken.set):
        woken.clear()
        async with migrated_sessions() as db:
            await db.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE pid <> pg_backend_pid() AND query LIKE 'LISTEN%matrix_outbox_wakes%'"
                )
            )
        async with asyncio.timeout(30):
            await woken.wait()


async def test_an_enqueue_wakes_the_running_drain(
    session_store, migrated_sessions, outbox_wakes, outbox, binding, session_id, turn_id, attachment_id
) -> None:
    """End to end off the wake alone: the backstop here is minutes, so a drain that still needed
    its poll to find work would time this test out rather than say the reply."""
    homeserver = _Homeserver()
    pacer = RoomPacer(sends_per_second=1e6, burst=100)
    drain = RoomOutboxDrain(outbox, pacer, homeserver.post, binding, outbox_wakes, backstop=timedelta(minutes=5))

    async with pacer.run(), drain.run():
        async with asyncio.timeout(30):
            # An enqueue landing before the drain's first pass is found by that pass; one landing
            # after it is parked behind the wake wait, where only the wake can deliver it inside
            # this timeout — the clear-before-pass ordering covers the mid-pass interleaving.
            await _enqueue(session_store, migrated_sessions, outbox, attachment_id, session_id, turn_id, "the answer")
            await homeserver.said.wait()

    assert homeserver.posted == ["the answer"]


async def _due_now(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Bring every unsent row's retry forward, so a test waits on outcomes rather than on clocks."""
    async with sessions() as db, db.begin():
        for row in await db.scalars(select(MatrixOutbox).where(MatrixOutbox.sent_at.is_(None))):
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


if __name__ == "__main__":
    pytest_bazel.main()
