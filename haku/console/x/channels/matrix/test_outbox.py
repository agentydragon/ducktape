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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.database_schema import SessionOutbox
from haku.console.x.channels.matrix.client import MatrixError, RoomEventKind
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM
from haku.console.x.channels.matrix.outbox import MAX_SEND_ATTEMPTS, PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.x.channels.matrix.pacer import RoomPacer
from haku.console.x.session_store import BridgeAuthentication, MatrixSession, SessionStore, SpaSession


@pytest.fixture
def outbox(migrated_sessions: async_sessionmaker[AsyncSession]) -> RoomOutbox:
    return RoomOutbox(migrated_sessions)


@pytest.fixture
async def session_id(chat_store: SessionStore, operator_id: UUID) -> UUID:
    """A live Matrix session for `MATRIX_ROOM`, since an outbox row is one of its children."""
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=MATRIX_ROOM))
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


@pytest.fixture
async def turn_id(chat_store: SessionStore, operator_id: UUID, session_id: UUID) -> UUID:
    """The exchange these replies are produced in: a message is opened inside a turn, which is
    what records that the turn has queued one (`session_turns.queued_reply`)."""
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    return turn.turn_id


class _Homeserver:
    """What the drain sends through, refusing whatever it has been told to refuse."""

    def __init__(self, *, refuses: set[str] | None = None) -> None:
        self.posted: list[str] = []
        self.transactions: list[str] = []
        self._refuses = refuses or set()

    async def post(self, reply: PendingReply) -> None:
        self.transactions.append(reply.transaction_id())
        if reply.body in self._refuses:
            raise MatrixError("429: slow down")
        self.posted.append(reply.body)

    def accepts_everything(self) -> None:
        self._refuses = set()


def _unpaced(engine: AsyncEngine, outbox: RoomOutbox, homeserver: _Homeserver) -> tuple[RoomPacer, RoomOutboxDrain]:
    """A drain over a pacer with effectively no rate, so a test waits on outcomes not on tokens.

    The rate itself is `pacer`'s and is asserted there; what is under test here is what
    the rows say afterwards.
    """
    pacer = RoomPacer(sends_per_second=1e6, burst=100)
    return pacer, RoomOutboxDrain(engine, outbox, pacer, homeserver.post, _room)


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[SessionOutbox]:
    async with sessions() as db:
        return list(await db.scalars(select(SessionOutbox).order_by(SessionOutbox.created_at)))


async def _enqueue(chat_store: SessionStore, session_id: UUID, turn_id: UUID, *bodies: str) -> None:
    """Produce *bodies* the way a turn does: one completed assistant message each."""
    for frame_seq, body in enumerate(bodies, start=1):
        assert await chat_store.update_assistant(
            session_id,
            await chat_store.begin_assistant(session_id, turn_id, source_first_frame_seq=frame_seq),
            body,
            complete=True,
        )


async def test_a_reply_is_said_once_and_then_never_again(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id
) -> None:
    """The ordinary path, and the half of `exactly once` that a redrive could break: a row the
    homeserver accepted is `sent_at` and never claimed again."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()
        assert not await drain.drain_once(), "a sent row was claimed a second time"

    assert homeserver.posted == ["the answer"]
    [row] = await _rows(migrated_sessions)
    assert (row.sent_at is not None, row.attempts, row.last_error) == (True, 1, None)


async def test_replies_are_said_in_the_order_they_were_produced(
    chat_store, migrated_engine, outbox, session_id, turn_id
) -> None:
    """A turn that narrates, works and reports back is three rows, and the room reads top to
    bottom: out of order they describe a different turn."""
    homeserver = _Homeserver()
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "looking now", "found it", "fixed")

    async with pacer.run():
        while await drain.drain_once():
            pass

    assert homeserver.posted == ["looking now", "found it", "fixed"]


async def test_a_refused_send_leaves_the_row_for_the_next_attempt(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id
) -> None:
    """The drop this table exists for (<../../../debug/message_drops.md> E1).

    A queued send that raised used to be popped and discarded with a log line, and the turn had
    already recorded the room as having heard it. Here the failure is the row's: unsent, one
    attempt spent, the homeserver's own words kept, and claimable again once its backoff passes.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "the answer")

    async with pacer.run():
        assert await drain.drain_once()
        await pacer.flush()

    assert homeserver.posted == []
    [row] = await _rows(migrated_sessions)
    assert row.sent_at is None, "a send that raised must not count as delivered"
    assert (row.attempts, row.last_error) == (1, "429: slow down")


async def test_a_reply_the_room_refused_is_said_once_the_homeserver_relents(
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id
) -> None:
    """R11.6: a produced reply is retried rather than lost. The wait is skipped by hand, because
    what is under test is that the row comes back at all and not how long it waits first."""
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "the answer")

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
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id
) -> None:
    """Order survives a retry, which is the half of this that a due-rows-only query would lose.

    A reply waiting out its backoff is not overtaken by the next one: the room is read top to
    bottom, so an answer arriving before the answer it follows describes a different turn.
    """
    homeserver = _Homeserver(refuses={"found it"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "found it", "and fixed it")

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
    chat_store, migrated_sessions, migrated_engine, outbox, session_id, turn_id, caplog
) -> None:
    """A room that has refused the same message eight times is not going to take the ninth.

    So this is the one row the ordered queue steps over: kept, unsent, with the reason on it —
    a message nobody could say is still one an operator can find — while the reply behind it,
    which the head-of-line rule would otherwise strand forever, goes out.
    """
    homeserver = _Homeserver(refuses={"the answer"})
    pacer, drain = _unpaced(migrated_engine, outbox, homeserver)
    await _enqueue(chat_store, session_id, turn_id, "the answer", "and the next one")

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


async def test_one_message_is_queued_once_however_often_its_frame_arrives(
    chat_store, migrated_sessions, session_id, turn_id
) -> None:
    """A runner replaying its rollout into a replacement replica offers the same completed
    `assistant` frame again. Without the partial unique index that would be a second row for one
    transcript message, and the room would read the answer twice."""
    message_id = await chat_store.begin_assistant(session_id, turn_id, source_first_frame_seq=1)

    for _ in range(3):
        assert await chat_store.update_assistant(session_id, message_id, "the answer", complete=True)

    [row] = await _rows(migrated_sessions)
    assert (row.message_id, row.body) == (message_id, "the answer")


async def test_a_turns_last_word_is_queued_once_however_often_the_turn_is_adopted(
    chat_store, migrated_sessions, session_id, turn_id
) -> None:
    """The duplicate the `message_id` index cannot catch, because these rows have none.

    A turn's last word — `result.result` on a turn whose completed messages were all empty — is
    written *before* the turn is closed, so a replica dying in that window
    leaves the turn open and its replacement re-derives the same reply. `turn_id` is what makes the
    second derivation a no-op; without it the fix for a lost reply would have introduced a
    duplicated one.
    """
    for _ in range(3):
        assert await chat_store.enqueue_turn_reply(session_id, turn_id, "[stopped by the operator]")

    [row] = await _rows(migrated_sessions)
    assert (row.turn_id, row.message_id, row.body) == (turn_id, None, "[stopped by the operator]")
    assert (await chat_store.turn_state(turn_id)).queued_reply, "the turn records the row it queued"


async def test_a_session_serving_no_room_queues_nothing(chat_store, migrated_sessions, operator_id) -> None:
    """The SPA reads the message rows over SSE, so a finished turn is delivered by being written
    down. A row for it would be a reply nothing will ever say."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    message_id = await chat_store.begin_assistant(view.session_id, turn.turn_id, source_first_frame_seq=1)

    assert not await chat_store.update_assistant(view.session_id, message_id, "the answer", complete=True)

    assert await _rows(migrated_sessions) == []
    state = await chat_store.turn_state(turn.turn_id)
    assert (state.said_anything, state.queued_reply) == (True, False), "it spoke; the room is owed nothing"


async def test_the_tag_says_which_transcript_row_the_room_is_showing(
    chat_store, migrated_sessions, session_id, turn_id
) -> None:
    """Rebuilt from the row rather than stored beside it, so the two cannot disagree."""
    message_id = await chat_store.begin_assistant(session_id, turn_id, source_first_frame_seq=1)
    await chat_store.update_assistant(session_id, message_id, "the answer", agent_message_id="msg_01abc", complete=True)
    [row] = await _rows(migrated_sessions)

    tag = PendingReply(
        outbox_id=row.outbox_id,
        session_id=row.session_id,
        room_id=row.room_id,
        body=row.body,
        message_id=row.message_id,
        agent_message_id=row.agent_message_id,
        attempts=row.attempts,
    ).tag()

    assert (tag.kind, tag.session_id, tag.message_id, tag.agent_message_id) == (
        RoomEventKind.REPLY,
        session_id,
        message_id,
        "msg_01abc",
    )


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


async def _never_posted(reply: PendingReply) -> None:
    raise AssertionError(f"nothing should have been sent, but {reply.body!r} was")


async def _due_now(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Bring every unsent row's retry forward, so a test waits on outcomes rather than on clocks."""
    async with sessions() as db, db.begin():
        for row in await db.scalars(select(SessionOutbox).where(SessionOutbox.sent_at.is_(None))):
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


if __name__ == "__main__":
    pytest_bazel.main()
