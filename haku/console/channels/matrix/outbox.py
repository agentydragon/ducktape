"""The room's outbox: replies as rows, and the one task that says them.

**The channel writes the row, reading the log forward from its own cursor**
(`conversation_subscriber.ConversationSubscriber`), and this drains it. `sent_at` is written only once `room_send`
has returned, so every other outcome, the replica disappearing mid-send included, leaves the row
claimable by whoever comes next.

**`pacer` owns when.** The drain does not send; it queues one reply into the pacer and waits for
that closure to settle before claiming the next. So replies keep the room's rate budget, their
order among themselves, and their interleaving with the status line and the lifecycle notices —
which stay in-process on purpose, since a notice describing a moment is not worth redelivering ten
minutes later.

**One drainer per attachment.** Two drains on one attachment would reorder replies against each
other, so each attachment's reconciler runs exactly one, and the reconcilers run only on the sync
leader — the election is the sync loop's, not this module's.

**The enqueue's own transaction wakes the drain, on the channel's own wire** (<outbox_wake.py>),
because nothing earlier can: the conversation wake that made the enqueueing subscriber read has
already fired by the time the row exists, so a drain woken by that wake would look before the row
was there and sleep past it. Emitting inside the insert's transaction closes the race exactly:
`pg_notify` delivers on commit, so the wake the drain acts on cannot precede the row it announces.
The wire is the channel's own because the fact is: an outbox row is this channel's delivery state,
not a conversation development, so its wake has no business on the conversation channel. Delivery
stays at-most-once however the listener fares, and a row's send backoff expires by clock, which no
wake announces — both are what `WAKE_BACKSTOP` is for.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState, Retrying, wait_exponential

from haku.console.channels.matrix.client import EventTag, RoomEventKind
from haku.console.channels.matrix.conversation import RoomAttachment
from haku.console.channels.matrix.outbox_wake import OutboxWakes, notify_outbox
from haku.console.channels.matrix.pacer import MAX_QUEUED_SENDS, SENDS_PER_SECOND, RoomPacer
from haku.console.chat_models import ItemStatus, ItemType
from haku.console.database_schema import ConversationItem, MatrixOutbox

logger = logging.getLogger(__name__)

# How many times one reply may be attempted before it is left alone. Past this the row stops being
# claimed and keeps its `last_error`: a queue that retries one reply forever never gets to the next.
# Deliberately not a delete — a reply nobody could say is still somewhere an operator can find it.
MAX_SEND_ATTEMPTS = 8

# The retry curve for a refused reply: five seconds after the first failure, doubling to five
# minutes. The whole budget — roughly ten minutes across `MAX_SEND_ATTEMPTS` — stays inside the
# 30-to-60 minutes Synapse keeps a transaction id for (<../../docs/chat_runtime_facts.md>); past
# that window a redrive stops being deduplicated and starts being a second message.
_RETRY_CURVE = wait_exponential(multiplier=5, max=datetime.timedelta(seconds=300))

# The backstop behind the wake, for what no notification reaches us with: delivery is at-most-once
# however the listener fares, and a row waiting out its send backoff comes due by clock, which no
# wake announces. Matches the notices reader's own backstop; relying on it costs a retry landing up
# to this much after its backoff, still far inside the transaction-id window the retry budget is
# sized against.
WAKE_BACKSTOP = datetime.timedelta(seconds=10)

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
    attachment_id: UUID
    room_id: str
    subject: str
    body: str
    attempts: int

    def tag(self) -> EventTag:
        """What the room event states about itself."""
        return EventTag(kind=RoomEventKind.REPLY)

    def transaction_id(self) -> str:
        """What this reply is sent under, on every attempt.

        **The row's own id, not `EventTag.transaction_id`'s**, which mints a fresh uuid4 every call
        — under which a redrive would post a second message instead of being refused. The row id is
        stable for exactly as long as redelivery can happen.
        """
        return self.outbox_id.hex


def _pending(row: MatrixOutbox, *, room_id: str) -> PendingReply:
    return PendingReply(
        outbox_id=row.outbox_id,
        attachment_id=row.attachment_id,
        room_id=room_id,
        subject=row.subject,
        body=row.body,
        attempts=row.attempts,
    )


def _backoff(attempts: int) -> datetime.timedelta:
    """Backoff before the retry that follows the ``attempts``-th spent attempt.

    Tenacity is the wait calculator only (the `oauth.token_state._refresh_retry_delay` pattern): the
    retry loop itself is the drain re-claiming the row across processes and restarts, so the row's
    `next_attempt_at` carries the schedule between them and a minimal `RetryCallState` stands in
    for the live loop tenacity would otherwise drive.
    """
    retry_state = RetryCallState(retry_object=Retrying(), fn=None, args=(), kwargs={})
    retry_state.attempt_number = attempts
    return datetime.timedelta(seconds=_RETRY_CURVE(retry_state))


class RoomOutbox:
    """Reads and writes over `matrix_outbox`. Says nothing about credentials.

    **Keyed by the attachment**, because what owes a room is the channel holding the copy rather
    than whichever session produced the words. A replacement session under the same thread inherits
    the queue instead of starting a new one.

    **`subject` is the row's own column now**, not derived from which of two identities it carried.
    Its predecessor needed that fork because a turn could produce a reply no transcript row held;
    prose is only ever segments of an item, so that case is gone and the idempotence key is stored
    once where the unique index can see it. For a reply that subject is the item's id.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def enqueue(self, attachment_id: UUID, item_id: UUID) -> bool:
        """Owe the room this item's prose. False where there was nothing to owe it.

        **The text is read here, not passed in.** A subscriber queues an item because it saw the
        item complete, and a complete item's `text` is final — so taking it from the caller would be
        the same prose in two places with two ways to disagree.

        **Queued once per subject.** A subscriber that crashed between sending and keeping its
        position sees the same completion again, and so does a runner replaying its rollout into a
        replacement replica; the room must not hear the answer twice, and the unique index is what
        says so rather than the caller remembering.

        An item that finished with nothing in it is not a reply: a turn that only ran tools said
        nothing, and an empty room event would be the console reporting that as an answer.

        **A row this call created wakes the drain, from this same transaction** — see the module
        docstring for why nothing earlier can. A conflict wakes nobody: the enqueue that inserted
        the row already did.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            item = await db.get(ConversationItem, item_id)
            if item is None or item.item_type is not ItemType.MESSAGE or item.status is not ItemStatus.COMPLETE:
                return False
            if not (body := item.item_text.strip()):
                return False
            queued = await db.scalar(
                insert(MatrixOutbox)
                .values(
                    outbox_id=uuid4(),
                    attachment_id=attachment_id,
                    subject=item_id.hex,
                    body=body,
                    created_at=now,
                    attempts=0,
                    next_attempt_at=now,
                )
                .on_conflict_do_nothing(index_elements=["attachment_id", "subject"])
                # What says whether the row is ours or one a previous pass already queued. A
                # conflict returns nothing, which is the answer rather than an error.
                .returning(MatrixOutbox.outbox_id)
            )
            if queued is None:
                return False
            await notify_outbox(db)
            return True

    async def claim_next(self, binding: RoomAttachment) -> PendingReply | None:
        """Take this attachment's oldest live reply if it is due, counting the attempt as spent.

        **A failed reply halts the queue rather than being overtaken.** The row asked for is the
        oldest, not the oldest that happens to be due, so a reply waiting out its backoff holds up
        the one behind it: the room is read top to bottom, and two answers arriving in the wrong
        order describe a conversation that did not happen. The one row skipped is one out of
        attempts, which will never be sent and would otherwise wedge everything behind it forever.

        The attempt is charged here rather than after the send, so a replica disappearing
        mid-request costs the row one attempt instead of leaving it claimable forever by processes
        that keep dying on it. `sent_at` is still the only record of success.

        Only ever called by the attachment's one drain, so the `FOR UPDATE` is against a
        concurrent enqueue rather than another drain.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            row = await db.scalar(
                select(MatrixOutbox)
                .where(
                    MatrixOutbox.attachment_id == binding.attachment_id,
                    MatrixOutbox.sent_at.is_(None),
                    MatrixOutbox.attempts < MAX_SEND_ATTEMPTS,
                )
                .order_by(MatrixOutbox.created_at)
                .limit(1)
                .with_for_update()
            )
            if row is None or row.next_attempt_at > now:
                return None
            row.attempts += 1
            row.next_attempt_at = now + _backoff(row.attempts)
            return _pending(row, room_id=binding.room_id)

    async def mark_sent(self, outbox_id: UUID) -> None:
        """Record that the room has this reply.

        **No correspondence row.** Its predecessor wrote one per delivered message, which is a
        flushed-up-to position materialised one row at a time; `channel_cursor` holds that
        properly. What still earns a row is a subject the channel can go back and *edit*, and that
        is `revisions.py`.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            if (row := await db.get(MatrixOutbox, outbox_id)) is None:
                return
            row.sent_at = now
            row.last_error = None

    async def record_failure(self, outbox_id: UUID, error: str) -> None:
        """Keep why the room refused this reply, and say so once it is out of attempts."""
        async with self._sessions() as db, db.begin():
            if (row := await db.get(MatrixOutbox, outbox_id)) is None:
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


class RoomOutboxDrain:
    """Turns one attachment's rows back into room events, at the pace the room will take them.

    One per live attachment, run by its reconciler on the sync leader. Woken over the channel's own
    wire (`OutboxWakes`, one listener per process, shared by every drain): the enqueue's own
    transaction emits the wake, so the wake this drain acts on cannot precede the row it announces,
    and `WAKE_BACKSTOP` covers a wake the listener never saw and a retry coming due by clock.
    """

    def __init__(
        self,
        outbox: RoomOutbox,
        pacer: RoomPacer,
        post: PostReply,
        binding: RoomAttachment,
        wakes: OutboxWakes,
        *,
        backstop: datetime.timedelta = WAKE_BACKSTOP,
    ):
        self._outbox = outbox
        self._pacer = pacer
        self._post = post
        self._binding = binding
        self._wakes = wakes
        self._backstop = backstop
        self._changed = asyncio.Event()

    async def drain_once(self) -> bool:
        """Say the next reply this attachment's room is owed. False when there was nothing to say."""
        if (reply := await self._outbox.claim_next(self._binding)) is None:
            return False
        settled = asyncio.Event()

        async def post() -> None:
            try:
                await self._post(reply)
                await self._outbox.mark_sent(reply.outbox_id)
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

    async def _run(self) -> None:
        while True:
            try:
                # Cleared before the pass, so an enqueue committed while it runs wakes the next
                # one instead of being cleared away after it was already missed.
                self._changed.clear()
                if await self.drain_once():
                    continue
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._backstop.total_seconds()):
                        await self._changed.wait()
            except Exception:
                logger.exception("Matrix: draining the outbox of %s failed", self._binding.room_id)
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Hold the drain, registered on the shared wake wire; the wire's lifecycle is its owner's.

        A wake carries nothing to inspect and names no attachment, so every drain on the wire
        looks; a woken drain that owes nothing goes back to waiting.
        """
        with self._wakes.watch(self._changed.set):
            task = asyncio.create_task(self._run(), name=f"matrix-room-outbox-{self._binding.attachment_id}")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
