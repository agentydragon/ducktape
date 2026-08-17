"""The room's outbox: replies as rows, and the one task that says them.

A turn writes the row in the same transaction as the assistant message it copies — which also
covers a turn raising between producing text and speaking it — and this drains it. `sent_at` is
written only once `room_send` has returned, so every other outcome, the replica disappearing
mid-send included, leaves the row claimable by whoever comes next
(<../../../debug/message_drops.md> E1, E4, E6, E7).

**`pacer` owns when.** The drain does not send; it queues one reply into the pacer and waits for
that closure to settle before claiming the next. So replies keep the room's rate budget, their
order among themselves, and their interleaving with the status line and the lifecycle notices —
which stay in-process on purpose, since a notice describing a moment is not worth redelivering ten
minutes later.

**One drainer.** The pacer runs on every replica (the turn loop speaks from whichever holds the
session's lease), but two drains would reorder replies against each other, so this contends for an
advisory lock the way the sync loop and the supervisor do.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.database_schema import SessionOutbox
from haku.console.x import delivery_log
from haku.console.x.channels.matrix.client import EventTag, RoomEventKind
from haku.console.x.channels.matrix.pacer import MAX_QUEUED_SENDS, SENDS_PER_SECOND, RoomPacer
from haku.console.x.channels.matrix.session import live_attachment

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock, the supervisor's, and the OAuth refresh sweep's.
_OUTBOX_ADVISORY_LOCK = 0x4D58_4F42  # "MXOB"

# How many times one reply may be attempted before it is left alone. Past this the row stops being
# claimed and keeps its `last_error`: a queue that retries one reply forever never gets to the next.
# Deliberately not a delete — a reply nobody could say is still somewhere an operator can find it.
MAX_SEND_ATTEMPTS = 8

# The first wait after a failed attempt, doubling to `MAX_RETRY_BACKOFF`. The whole budget —
# roughly ten minutes across `MAX_SEND_ATTEMPTS` — stays inside the 30-to-60 minutes Synapse keeps a
# transaction id for (<../../../docs/chat_runtime_facts.md>); past that window a redrive stops being
# deduplicated and starts being a second message.
FIRST_RETRY_BACKOFF = datetime.timedelta(seconds=5)
MAX_RETRY_BACKOFF = datetime.timedelta(seconds=300)

# How long the drain waits before looking again when it found nothing. The room's own rate is one
# send per five seconds, so polling faster than this would buy latency nothing else can spend.
IDLE_POLL = datetime.timedelta(seconds=1)

# How long a replica that lost the election waits before contending again. Shorter than the sync
# loop's, because what waits out this interval after a roll is an answer the operator is looking
# at an empty room for.
LEADER_RETRY = datetime.timedelta(seconds=5)

# Backoff after the drain itself failed, so a database outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)

# A ceiling on waiting for one queued reply to be attempted — a backstop, not a timeout anyone
# expects to reach. `RoomPacer.send` drops silently once `MAX_QUEUED_SENDS` are waiting, so without
# this the drain could park forever on a closure that will never run. Sized to a full queue draining
# at the room's rate, so a reply waiting its turn behind a burst of narration is never re-claimed.
SETTLE_CEILING = datetime.timedelta(seconds=MAX_QUEUED_SENDS / SENDS_PER_SECOND)


@dataclass(frozen=True)
class PendingReply:
    """One row, as the thing that sends it needs to see it."""

    outbox_id: UUID
    session_id: UUID
    room_id: str
    body: str
    message_id: UUID | None
    agent_message_id: str | None
    turn_id: UUID | None
    attempts: int

    def subject(self) -> str:
        """What the room shows here, as `chat_delivery` keys it.

        **The record's identity, never the outbox row's**, because the queue is the channel's
        private implementation and a reconciler re-deriving this reply from the transcript has to
        arrive at the same subject after the table is gone.
        """
        match self.message_id, self.turn_id:
            case UUID() as message_id, _:
                return f"message:{message_id.hex}"
            case _, UUID() as turn_id:
                return f"turn:{turn_id.hex}"
            case _:
                raise ValueError(f"outbox row carries neither identity: {self.outbox_id=}")

    def tag(self) -> EventTag:
        """What the room event states about itself.

        Rebuilt from the row rather than stored beside it: a reply's tag is exactly these columns,
        so a JSON copy would be the same facts twice with two ways to disagree.
        """
        return EventTag(
            kind=RoomEventKind.REPLY,
            session_id=self.session_id,
            message_id=self.message_id,
            agent_message_id=self.agent_message_id,
        )

    def transaction_id(self) -> str:
        """What this reply is sent under, on every attempt.

        **The row's own id, not `EventTag.transaction_id`'s.** That mints a fresh uuid4 where the
        event names no transcript row, so a redrive of the one reply naming none (`result.result`
        on a turn whose completed messages were all empty) would post a second message instead of
        being refused. The row id is stable for exactly as long as redelivery can happen.
        """
        return self.outbox_id.hex


def _pending(row: SessionOutbox) -> PendingReply:
    return PendingReply(
        outbox_id=row.outbox_id,
        session_id=row.session_id,
        room_id=row.room_id,
        body=row.body,
        message_id=row.message_id,
        agent_message_id=row.agent_message_id,
        turn_id=row.turn_id,
        attempts=row.attempts,
    )


def _backoff(attempts: int) -> datetime.timedelta:
    doublings: int = 2 ** max(attempts - 1, 0)
    return min(FIRST_RETRY_BACKOFF * doublings, MAX_RETRY_BACKOFF)


class RoomOutbox:
    """Reads and writes over `session_outbox`. Says nothing about rooms or credentials."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def claim_next(self, room_id: str) -> PendingReply | None:
        """Take this room's oldest live reply if it is due, counting the attempt as spent.

        **A failed reply halts the queue rather than being overtaken.** The row asked for is the
        oldest, not the oldest that happens to be due, so a reply waiting out its backoff holds up
        the one behind it: the room is read top to bottom, and two answers arriving in the wrong
        order describe a conversation that did not happen. The one row skipped is one out of
        attempts, which will never be sent and would otherwise wedge everything behind it forever.

        The attempt is charged here rather than after the send, so a replica disappearing
        mid-request costs the row one attempt instead of leaving it claimable forever by processes
        that keep dying on it. `sent_at` is still the only record of success.

        Only ever called under the drain's advisory lock, so the `FOR UPDATE` is against a
        concurrent enqueue rather than another drain.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            row = await db.scalar(
                select(SessionOutbox)
                .where(
                    SessionOutbox.room_id == room_id,
                    SessionOutbox.sent_at.is_(None),
                    SessionOutbox.attempts < MAX_SEND_ATTEMPTS,
                )
                .order_by(SessionOutbox.created_at)
                .limit(1)
                .with_for_update()
            )
            if row is None or row.next_attempt_at > now:
                return None
            row.attempts += 1
            row.next_attempt_at = now + _backoff(row.attempts)
            return _pending(row)

    async def mark_sent(self, outbox_id: UUID, subject: str, sent_ref: str) -> None:
        """Record that the room has this reply, and which event of the room's it is.

        One transaction, so `sent_at` and the correspondence cannot come apart: a reply the room
        accepted whose event we did not write down is one a reconciler could only find by reading
        the room back.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            if (row := await db.get(SessionOutbox, outbox_id)) is None:
                return
            row.sent_at = now
            row.last_error = None
            if (attachment_id := await live_attachment(db, row.room_id)) is None:
                logger.warning("Matrix: %s has no live attachment, not recording what was sent there", row.room_id)
                return
            db.add(delivery_log.sent(attachment_id=attachment_id, subject=subject, sent_ref=sent_ref, now=now))

    async def record_failure(self, outbox_id: UUID, error: str) -> None:
        """Keep why the room refused this reply, and say so once it is out of attempts."""
        async with self._sessions() as db, db.begin():
            if (row := await db.get(SessionOutbox, outbox_id)) is None:
                return
            row.last_error = error
            if row.attempts >= MAX_SEND_ATTEMPTS:
                # Loud, because this is the one outcome the table cannot recover from by itself.
                # The row stays unsent with this error on it, still readable and still redrivable
                # by hand once whatever refused it has been fixed.
                logger.error(
                    "Matrix: giving up on outbox row %s after %d attempts; last error: %s",
                    outbox_id,
                    row.attempts,
                    error,
                )


# Saying one reply into the room, and answering with the room's own reference for the event it
# became. Raising is how the sender reports a refusal, which leaves the row claimable again.
PostReply = Callable[[PendingReply], Awaitable[str]]

# The room this console is currently bound to, or None before there is one.
BoundRoom = Callable[[], Awaitable[str | None]]


class RoomOutboxDrain:
    """Turns rows back into room events, at the pace the room will take them."""

    def __init__(self, engine: AsyncEngine, outbox: RoomOutbox, pacer: RoomPacer, post: PostReply, room: BoundRoom):
        self._engine = engine
        self._outbox = outbox
        self._pacer = pacer
        self._post = post
        self._room = room

    async def drain_once(self) -> bool:
        """Say the next reply the bound room is owed. False when there was nothing to say."""
        if (room_id := await self._room()) is None:
            return False
        if (reply := await self._outbox.claim_next(room_id)) is None:
            return False
        settled = asyncio.Event()

        async def post() -> None:
            try:
                await self._outbox.mark_sent(reply.outbox_id, reply.subject(), await self._post(reply))
            except Exception as error:
                # Recorded and re-raised rather than handled: `pacer` logs it and learns a
                # 429's `retry_after_ms` from it, and only the row can carry it past this process.
                await self._outbox.record_failure(reply.outbox_id, str(error))
                raise
            finally:
                settled.set()

        self._pacer.send(post)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(settled.wait(), SETTLE_CEILING.total_seconds())
        return True

    async def _drain_as_leader(self) -> None:
        """Drain until cancelled. Only ever entered holding the advisory lock."""
        while True:
            try:
                if not await self.drain_once():
                    await asyncio.sleep(IDLE_POLL.total_seconds())
            except Exception:
                logger.exception("Matrix: draining the room outbox failed")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    async def _run(self) -> None:
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _OUTBOX_ADVISORY_LOCK}):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica drains the room outbox")
                try:
                    await self._drain_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: the outbox drain exited, retrying")
                    await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _OUTBOX_ADVISORY_LOCK})

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        task = asyncio.create_task(self._run(), name="matrix-room-outbox")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
