"""The room's position in the conversation, and the notices it owes from it.

The Matrix half of <../../subscription.py>: the channel reads the record from a position of its own
instead of being handed events by whoever happens to be running the turn.

- **`RoomCursor`** is the durable position, in `matrix_room_cursor`. The room holds a federated copy
  that outlives every console process, so after a restart the channel has to know what it already
  put there. That durability is Matrix's own problem, kept in Matrix's own table below the channel
  boundary and beside the outbox; the conversation layer has no cursor table at all.
- **`RoomNotices`** is the subscriber. It wakes on `session_changed`, reads what the room has not
  been told, says it, and keeps the position it reached.

**Replies and notices, from one position.** A completed message becomes a `matrix_outbox` row the
drain says into the room — with a transaction id, a retry budget and an ordering guarantee against
other answers, none of which a notice needs — and everything else is said straight from here. Both
come off the same cursor, so a notice can no longer overtake the answer it was about; what the turn
loop used to write inside its own transaction is now something the channel decides for itself.

**Every kind the stream carries — which is not every kind the room shows.** What `notice` reads is
what `conversation_event` records, and several things the room says have no row at all: a room being
bound or adopted, an invite refused. Those still reach the room by being pushed at it.

**The position is kept after the notice, never before.** A crash in that window says the notice
again on the next pass — the same trade `revisions.retire` takes, and the right way round: a
room told twice is odd, a room never told is a message silently dropped. What is left unguarded is
the pacer's in-process queue, exactly as every other notice already is.
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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    ItemType,
    LeaseExpiryReason,
    MatrixOrigin,
    PromptOrigin,
    PromptRejection,
    SpaOrigin,
    TurnOutcome,
)
from haku.console.database_schema import ChannelCursor, ConversationItem
from haku.console.x.channels.matrix.client import RoomEventKind
from haku.console.x.channels.matrix.conversation import MatrixConversationStore
from haku.console.x.channels.matrix.outbox import BoundRoom, RoomOutbox
from haku.console.x.session_events import (
    LeaseExpiredBody,
    MessageCompletedBody,
    MessageStartedBody,
    PromptCompletedBody,
    PromptRejectedBody,
    PromptStartedBody,
    ReasoningCompletedBody,
    ReasoningStartedBody,
    SegmentBody,
    SessionAdoptedBody,
    SessionEndedBody,
    SessionProvisioningBody,
    SetupNarrationBody,
    ToolCallCompletedBody,
    ToolCallStartedBody,
    TurnEndedBody,
    TurnStartedBody,
    UnknownEventBody,
    UnreadableInputBody,
)
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.subscription import ConversationStream, StreamedEvent, StreamPosition, Subscription, Unstarted

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock, the supervisor's, the outbox drain's and the OAuth sweep's.
_NOTICES_ADVISORY_LOCK = 0x4D58_4E54  # "MXNT"

# Saying one notice into a room. Wider than `conversation.Announce`, whose caller is the supervisor
# and always means the same kind: what this renders spans several, and the kind is what the room
# event states about itself.
Notify = Callable[[str, RoomEventKind], Awaitable[None]]

# How this room renders a `turn_aborted` event. The words are the channel's own: what is recorded is
# that the turn was aborted, and every channel gets to say so differently.
ABORTED_BY_OPERATOR = "[aborted by operator]"

# What marks a prompt the operator sent somewhere else. The room shows it because a prompt is a
# conversation fact and every attached surface shows one, and marks it because an operator reading
# this room would otherwise find a message they did not send here.
RELAYED_PROMPT = "[sent from another surface] "

# How many events one pass renders. Small, because each one it does render costs the room a send.
NOTICE_BATCH = 50

# The backstop for what no notification reaches us with: `SessionNotifications.watch` cannot replay
# the ids that arrived while its listener was reconnecting, so a woken reader still has to look on
# its own.
POLL_INTERVAL = datetime.timedelta(seconds=10)

# How long a replica that lost the election waits before contending again.
LEADER_RETRY = datetime.timedelta(seconds=30)

# Backoff after a failed pass, so a database outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


class RoomCursor:
    """Where this room's copy has been brought up to — `subscription.Cursor`, made durable.

    **Keyed by the attachment**, which is the one piece of channel state the conversation layer
    keeps generic: a position in the log is the resume contract every attached channel owes it, and
    the same integer answers it for every channel. Keying by room id instead made a channel join its
    position to its deliveries through its own public address.

    Absent means *never read*, not *at the start*, which is the distinction the reader needs: a
    room the console has been servicing since before this table existed already shows everything
    said in it, so replaying from zero would repeat the whole conversation into it.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], attachment_id: UUID) -> None:
        self._sessions = sessions
        self._attachment_id = attachment_id

    async def position(self) -> StreamPosition | None:
        async with self._sessions() as db:
            # Annotated because `AsyncSession.scalar` is typed `Any`, which `warn_return_any` refuses.
            reached: int | None = await db.scalar(
                select(ChannelCursor.event_seq).where(ChannelCursor.attachment_id == self._attachment_id)
            )
            return None if reached is None else StreamPosition(event_seq=reached)

    async def keep(self, position: StreamPosition) -> None:
        """Record that everything through *position* has reached the room.

        Upserted rather than read-then-written: the row is created by whichever replica holds the
        notices lock first, and an election that changes hands mid-conversation must not fail on the
        primary key.
        """
        async with self._sessions() as db, db.begin():
            await db.execute(
                insert(ChannelCursor)
                .values(attachment_id=self._attachment_id, event_seq=position.event_seq)
                .on_conflict_do_update(index_elements=["attachment_id"], set_={"event_seq": position.event_seq})
            )


@dataclass(frozen=True, slots=True)
class Notice:
    """One thing to say in the room, and what the room event states about itself."""

    body: str
    kind: RoomEventKind


def _why_not(reason: PromptRejection) -> str:
    """What a rejection says the operator is waiting for.

    The channel's own rendering of `PromptRejection`, hence here rather than beside the enum. A
    match rather than a lookup, so a member added later fails the type check instead of the send.
    """
    match reason:
        case PromptRejection.NO_SESSION:
            return "there is no session behind this room yet"
        case PromptRejection.SESSION_NOT_READY:
            return "Haku's sandbox is not up yet"
        case PromptRejection.TURN_IN_FLIGHT:
            return "Haku is still working on the previous message"
        case PromptRejection.PROMPT_QUEUED:
            return "a message is already waiting to be answered"


def _why_it_lapsed(reason: LeaseExpiryReason) -> str:
    """The room's own words for a lease that ran out, not `session_store._expiry_detail`'s.

    The holder is left out on purpose: a replica name is the console's own topology, and what the
    operator can act on is that the session is gone.
    """
    match reason:
        case LeaseExpiryReason.HOLDER_GONE:
            return "the console replica serving it went away"
        case LeaseExpiryReason.UNADOPTED:
            return "its sandbox went away and nothing took it back over"
        case LeaseExpiryReason.NEVER_ATTACHED:
            return "its sandbox never came up"


def _arrived_here(origin: PromptOrigin, room_id: str) -> bool:
    """Whether this prompt is already in this room because it was typed into it.

    An equality test against the address, never a look inside one: `MatrixOrigin`'s strings are the
    Matrix channel's own, and this is the channel that minted them.
    """
    match origin:
        case MatrixOrigin(address=address):
            return address == room_id
        case SpaOrigin():
            return False


def notice(event: StreamedEvent, *, room_id: str) -> Notice | None:
    """What this room says about *event*, or nothing where it says nothing.

    A match on the event's body rather than on its kind, and every shape spelled out rather than
    left to a wildcard, so a shape added to the stream lands here as a type error instead of being
    silently ignored.

    The shapes with a `None` arm are the ones the room shows some other way: an assistant message is
    an answer `reconcile_once` queues on the outbox rather than announces, and reasoning and tool
    calls are what the work notice will summarise (<../../../plans/conversation_layers.md> § 4)
    rather than a line each.

    `UnknownEventBody` is on that arm too, and it is a different statement: a kind a **newer**
    release wrote, which this one has no words for. The room says nothing about it and the cursor
    moves past it, so that line is never said — deliberately, and it is the arm to think about
    before adding a kind whose notice matters. Holding the cursor instead would leave the room
    silent for the whole roll, since this replica holds the notices election while it waits.
    """
    match event.body:
        case PromptRejectedBody(reason=reason):
            return Notice(f"not delivered — {_why_not(reason)}; send it again", RoomEventKind.REJECTED)
        case UnreadableInputBody(media_type=media_type):
            return Notice(
                f"received a message Haku cannot read ({media_type}) — it reads text only; "
                "describe it in words and it will reach the session",
                RoomEventKind.UNREADABLE,
            )
        case SessionAdoptedBody(holder=holder):
            return Notice(f"another console replica ({holder}) took this session over", RoomEventKind.LIFECYCLE)
        case LeaseExpiredBody(reason=reason):
            return Notice(f"the session ended — {_why_it_lapsed(reason)}", RoomEventKind.LIFECYCLE)
        case PromptStartedBody():
            # **The text is not on this row.** A prompt is an item and its prose is the segments
            # that follow, so the relay is said at the item's completion, where the whole of it is
            # readable — `reconcile_once`, beside the answer it is the mirror image of.
            return None
        case TurnEndedBody(outcome=TurnOutcome.ABORTED):
            # An abort is a turn outcome now rather than an event of its own, so the room's line for
            # it is said here — on the one outcome of three that the operator caused.
            return Notice(ABORTED_BY_OPERATOR, RoomEventKind.LIFECYCLE)
        case (
            MessageStartedBody()
            | ReasoningStartedBody()
            | ToolCallStartedBody()
            | SegmentBody()
            | MessageCompletedBody()
            | ReasoningCompletedBody()
            | ToolCallCompletedBody()
            | PromptCompletedBody()
            | TurnStartedBody()
            | TurnEndedBody()
            | SessionProvisioningBody()
            | SessionEndedBody()
            | SetupNarrationBody()
            | UnknownEventBody()
        ):
            return None


class RoomNotices:
    """Says what the record has recorded and the room has not been told, from the room's position.

    Answers included: a completed message is queued on the outbox rather than said from here, so it
    goes out under a transaction id with a retry budget, but the decision that the room is owed it
    is made here, in this order, off this cursor.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        stream: ConversationStream,
        conversations: MatrixConversationStore,
        notifications: SessionNotifications,
        announce: Notify,
        room: BoundRoom,
        outbox: RoomOutbox,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._stream = stream
        self._conversations = conversations
        self._notifications = notifications
        self._announce = announce
        self._room = room
        self._outbox = outbox
        self._changed = asyncio.Event()

    async def reconcile_once(self) -> bool:
        """Say or queue what the room is owed. True when the read stopped at its limit.

        **The position is kept after the whole batch, never during it.** A crash part-way replays
        the batch: a notice is said again, and a reply is refused by the outbox's unique subject —
        which is the trade this reader is built around.
        """
        if (room_id := await self._room()) is None:
            return False
        if (bound := await self._conversations.attachment_of_room(room_id)) is None:
            # A room this console holds no conversation for — never bound, or detached since — so
            # there is nothing recorded to be behind on.
            return False
        conversation_id, attachment_id = bound
        subscription = Subscription(self._stream, RoomCursor(self._sessions, attachment_id), conversation_id)
        read = await subscription.read(limit=NOTICE_BATCH)
        if isinstance(read, Unstarted):
            # Taken silently: the room already shows what was said in it for as long as it has been
            # bound, so rendering the stream from the start would repeat all of it.
            await subscription.keep(read.head)
            return False
        for event in read.events:
            match event.body:
                case MessageCompletedBody():
                    # The item's own text, read by the outbox: what the room is owed is the whole
                    # message, and this event deliberately carries none of it.
                    assert event.item_id is not None, "an item lifecycle row names its item"
                    await self._outbox.enqueue(attachment_id, event.item_id)
                case PromptCompletedBody():
                    assert event.item_id is not None, "an item lifecycle row names its item"
                    if (relayed := await self._relayed(event.item_id, room_id)) is not None:
                        await self._announce(relayed, RoomEventKind.NARRATION)
                case _:
                    if (said := notice(event, room_id=room_id)) is not None:
                        await self._announce(said.body, said.kind)
        await subscription.keep(read.position)
        return read.more

    async def _relayed(self, item_id: UUID, room_id: str) -> str | None:
        """A prompt the operator sent somewhere else, as this room should show it.

        The reader `origin` shipped without (#4289). A prompt is a conversation fact, so every
        attached surface shows it — but one typed here is already in the timeline above, and posting
        it again would show the operator their own sentence twice.
        """
        async with self._sessions() as db:
            item = await db.get(ConversationItem, item_id)
            if item is None or item.origin is None:
                return None
            origin = PromptStartedBody.model_validate({"item_type": ItemType.PROMPT, "origin": item.origin}).origin
            return None if _arrived_here(origin, room_id) else RELAYED_PROMPT + item.item_text

    def _wake(self, _session_id: UUID) -> None:
        """Note that some session's rows moved. Runs on the listener's reader task: no awaiting."""
        self._changed.set()

    async def _reconcile_as_leader(self) -> None:
        """Reconcile until cancelled. Only ever entered holding the advisory lock."""
        while True:
            try:
                # Cleared before the pass, so a change committed while it runs wakes the next one
                # instead of being cleared away after it was already missed.
                self._changed.clear()
                if not await self.reconcile_once():
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(POLL_INTERVAL.total_seconds()):
                            await self._changed.wait()
            except Exception:
                logger.exception("Matrix: reconciling the room's notices failed")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    async def _run(self) -> None:
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(
                    text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _NOTICES_ADVISORY_LOCK}
                ):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica says the room's notices")
                try:
                    await self._reconcile_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: the room's notice reader exited, retrying")
                    await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _NOTICES_ADVISORY_LOCK})

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        with self._notifications.watch(SessionEventKind.UPDATE, self._wake):
            task = asyncio.create_task(self._run(), name="matrix-room-notices")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
