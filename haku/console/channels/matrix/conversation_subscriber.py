"""The Matrix channel's subscriber to the conversation record, one per live attachment.

The Matrix half of <../../x/subscription.py>: the channel reads the record from a position of its own
instead of being handed events by whoever happens to be running the turn.

- **`RoomCursor`** is the durable position, in `channel_cursor`. The room holds a federated copy
  that outlives every console process, so after a restart the channel has to know what it already
  put there. That durability is Matrix's own problem, kept beside the outbox below the channel
  boundary; the conversation layer has no cursor table at all.
- **`ConversationSubscriber`** is the subscriber, constructed for one attachment and run by that
  attachment's reconciler on the sync leader. It wakes on its conversation's `update`, reads what
  the room has not been told, says it, and keeps the position it reached.

**Replies, notices and spans, from one position.** A completed message becomes a `matrix_outbox`
row the drain says into the room; a recorded fact becomes a sealed notice; and the editable lines —
a turn's work, a session's pre-turn life — are spans (<spans.py>) whose folded state is reconciled
after every batch. All of it comes off the same cursor, so a notice can no longer overtake the
answer it was about.

**Every kind the stream carries — which is not every kind the room shows.** What `project_notice`
and the span fold read is what `conversation_event` records, and the room-binding notices — a room
being bound or adopted, an invite refused — have no row at all. Those still reach the room by being
pushed at it from the sync loop.

**The position is kept after a sealed effect reaches the homeserver, never while it is merely
queued.** A crash in that window derives the notice again on the next pass — and the replay asks
the room's own copy first (`room_copy`): a source the room already shows is not sent at all,
however long ago the crash was, so the event-derived Matrix transaction id only has to cover the
gap between a send and its `/sync` echo. The right way round either way: a repeat is suppressed or
refused, while a room never told is a message silently dropped. Relayed prompts and silent-turn
narration ride the same path: their bodies read the record (the prompt item's text, the turn's
items) rather than being pure functions of one event, and a replay recomputes them from the same
rows. Span edits and retirements are the exception — level-triggered desired state, repaired by the
next pass or the takeover sweep rather than replayed, because an edit lost with a replica costs an
update the fold recomputes anyway.
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

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.matrix.client import RoomEventKind
from haku.console.channels.matrix.conversation import RoomAttachment
from haku.console.channels.matrix.outbox import RoomOutbox
from haku.console.channels.matrix.room_copy import RoomCopy
from haku.console.channels.matrix.spans import LiveSpans, RetireSpan, RoomFrontend, SealSpan
from haku.console.chat_models import (
    HarnessOrigin,
    ItemStatus,
    ItemType,
    MatrixOrigin,
    PromptOrigin,
    PromptRejection,
    SpaOrigin,
)
from haku.console.database_schema import ChannelCursor, ConversationItem
from haku.console.notifications.conversation_wakes import ConversationWakeEvent, ConversationWakes, RecheckHeld
from haku.console.session.subscription import ConversationStream, StreamedEvent, StreamPosition, Subscription, Unstarted
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
    TurnAbortedBody,
    TurnAnsweredBody,
    TurnFailedBody,
    TurnStartedBody,
    UnknownEventBody,
    UnreadableInputBody,
)

logger = logging.getLogger(__name__)

# A sealed notice said under a durable conversation event's identity. Returning means the
# homeserver accepted the effect, so the caller may advance its cursor.
ProjectNotice = Callable[[str, UUID, str, RoomEventKind, UUID, int], Awaitable[None]]

# A completed turn with no finished assistant message is legitimate, but silence looks like a lost
# answer. The turn loop records the facts; this subscriber supplies Matrix's words for their absence.
NOTHING_SAID = "the turn finished without saying anything"

# How this room renders a `turn_aborted` event. The words are the channel's own: what is recorded is
# that the turn was aborted, and every channel gets to say so differently.
ABORTED_BY_OPERATOR = "[aborted by operator]"

# What marks a prompt the operator sent somewhere else. The room shows it because a prompt is a
# conversation fact and every attached surface shows one, and marks it because an operator reading
# this room would otherwise find a message they did not send here.
RELAYED_PROMPT = "[sent from another surface] "

# How many events one pass renders. Small, because each one it does render costs the room a send.
NOTICE_BATCH = 50

# The backstop for what no notification reaches us with: delivery is at-most-once however the
# listener fares, so a woken reader still has to look on its own.
POLL_INTERVAL = datetime.timedelta(seconds=10)

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
            statement = insert(ChannelCursor).values(attachment_id=self._attachment_id, event_seq=position.event_seq)
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["attachment_id"],
                    set_={"event_seq": func.greatest(ChannelCursor.event_seq, statement.excluded.event_seq)},
                )
            )


@dataclass(frozen=True, slots=True)
class Notice:
    """One thing to say in the room, and what the room event states about itself."""

    body: str
    kind: RoomEventKind
    conversation_id: UUID
    source_event_seq: int


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


def _arrived_here(origin: PromptOrigin, room_id: str) -> bool:
    """Whether this prompt is already in this room because it was typed into it.

    An equality test against the address, never a look inside one: `MatrixOrigin`'s strings are the
    Matrix channel's own, and this is the channel that minted them.
    """
    match origin:
        case MatrixOrigin(address=address):
            return address == room_id
        case SpaOrigin() | HarnessOrigin():
            # The harness types into no room: a wake prompt reaches this room the same way an
            # SPA prompt does — by being posted, so the room learns the session woke itself.
            return False


def project_notice(event: StreamedEvent, *, conversation_id: UUID, room_id: str) -> Notice | None:
    """What this room seals about *event*, or nothing where it seals nothing.

    A match on the event's body rather than on its kind, and every shape spelled out rather than
    left to a wildcard, so a shape added to the stream lands here as a type error instead of being
    silently ignored.

    The shapes with a `None` arm are the ones the room shows some other way: an assistant message is
    an answer `reconcile_once` queues on the outbox rather than announces; reasoning and tool calls
    fold into the turn's work span; and the session lifecycle — provisioning, setup narration,
    adoption, endings — folds into the session's span (`spans.LiveSpans`), whose seal carries the
    lease-expiry words that used to be an arm here.

    `UnknownEventBody` is on that arm too, and it is a different statement: a kind a **newer**
    release wrote, which this one has no words for. The room says nothing about it and the cursor
    moves past it, so that line is never said — deliberately, and it is the arm to think about
    before adding a kind whose notice matters. Holding the cursor instead would leave the room
    silent for the whole roll, since this replica holds the notices election while it waits.
    """
    match event.body:
        case PromptRejectedBody(reason=reason):
            body = f"not delivered — {_why_not(reason)}; send it again"
            kind = RoomEventKind.REJECTED
        case UnreadableInputBody(media_type=media_type):
            body = (
                f"received a message Haku cannot read ({media_type}) — it reads text only; "
                "describe it in words and it will reach the session"
            )
            kind = RoomEventKind.UNREADABLE
        case PromptStartedBody():
            # **The text is not on this row.** A prompt is an item and its prose is the segments
            # that follow, so the relay is said at the item's completion, where the whole of it is
            # readable — `reconcile_once`, beside the answer it is the mirror image of.
            return None
        case TurnAbortedBody():
            # An abort is a turn outcome now rather than an event of its own, so the room's line for
            # it is said here — on the one outcome of three that the operator caused. The work
            # span's line is retired beside it: the abort is the fact, the status was live state.
            body = ABORTED_BY_OPERATOR
            kind = RoomEventKind.LIFECYCLE
        case TurnFailedBody(failure=failure):
            # The one ending a room cannot read from what it was sent. An answered turn arrives as
            # the answer and an aborted one as the line above; a failure produces no message at all,
            # so without this the conversation just stops mid-exchange and never says why.
            body = f"the turn failed — {failure}"
            kind = RoomEventKind.LIFECYCLE
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
            | TurnAnsweredBody()
            | SessionProvisioningBody()
            | SessionAdoptedBody()
            | SetupNarrationBody()
            | LeaseExpiredBody()
            | SessionEndedBody()
            | UnknownEventBody()
        ):
            return None
    return Notice(body=body, kind=kind, conversation_id=conversation_id, source_event_seq=event.position.event_seq)


class ConversationSubscriber:
    """Brings one attachment's room into agreement with the record, from the room's own position.

    Answers included: a completed message is queued on the outbox rather than said from here, so it
    goes out under a transaction id with a retry budget, but the decision that the room is owed it
    is made here, in this order, off this cursor. The editable lines come off the same read: the
    span fold's desired state is reconciled after the batch, and a span closed by a batch event is
    sealed or retired where the closing event is read.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        stream: ConversationStream,
        notifications: ConversationWakes,
        project: ProjectNotice,
        frontend: RoomFrontend,
        binding: RoomAttachment,
        outbox: RoomOutbox,
        room_copy: RoomCopy,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._stream = stream
        self._notifications = notifications
        self._project = project
        self._frontend = frontend
        self._binding = binding
        self._outbox = outbox
        self._room_copy = room_copy
        # Injectable because the span floors (`STATUS_AFTER`, `STATUS_EDIT_INTERVAL`) are wall-clock
        # rules a test cannot wait out; production passes nothing.
        self._clock = clock if clock is not None else _utc_now
        self._changed = asyncio.Event()
        self._state: LiveSpans | None = None
        self._swept = False

    async def reconcile_once(self) -> bool:
        """Say or queue what the room is owed. True when the read stopped at its limit.

        **The position is kept after the whole batch, never during it.** A crash part-way replays
        the batch: a sealed effect is suppressed by the room's own copy once its echo has been
        recorded and reuses its event-derived Matrix transaction until then, a reply is refused by
        the outbox's unique subject, and the span fold answers a replayed close from memory — which
        is the trade this reader is built around.
        """
        room_id = self._binding.room_id
        conversation_id, attachment_id = self._binding.conversation_id, self._binding.attachment_id
        subscription = Subscription(self._stream, RoomCursor(self._sessions, attachment_id), conversation_id)
        read = await subscription.read(limit=NOTICE_BATCH)
        if isinstance(read, Unstarted):
            # Taken silently: the room already shows what was said in it for as long as it has been
            # bound, so rendering the stream from the start would repeat all of it. The span fold
            # still catches up to that head, because its lines and typing are present-tense rather
            # than a replay into the timeline.
            state = await self._rebuilt(conversation_id, read.head)
            await state.reconcile(self._frontend, room_id, attachment_id, now=self._clock())
            await self._sweep_once(state, room_id, attachment_id)
            await subscription.keep(read.head)
            return False
        # The position this read started from, recovered from the events' own density — the first
        # returned row is exactly one past it — so the fold is rebuilt to the cursor and every
        # batch event is fresh to it.
        start = StreamPosition(event_seq=read.events[0].position.event_seq - 1) if read.events else read.position
        state = await self._rebuilt(conversation_id, start)
        for event in read.events:
            closes = state.advance(event)
            match event.body:
                case MessageCompletedBody():
                    # The item's own text, read by the outbox: what the room is owed is the whole
                    # message, and this event deliberately carries none of it.
                    assert event.item_id is not None, "an item lifecycle row names its item"
                    await self._outbox.enqueue(attachment_id, event.item_id)
                case PromptCompletedBody():
                    assert event.item_id is not None, "an item lifecycle row names its item"
                    if (relayed := await self._relayed(event.item_id, room_id)) is not None:
                        await self._deliver(
                            room_id,
                            attachment_id,
                            conversation_id,
                            relayed,
                            RoomEventKind.NARRATION,
                            event.position.event_seq,
                        )
                case TurnAnsweredBody():
                    assert event.turn_id is not None, "a turn lifecycle row names its turn"
                    if await self._silent(event.turn_id):
                        await self._deliver(
                            room_id,
                            attachment_id,
                            conversation_id,
                            NOTHING_SAID,
                            RoomEventKind.NARRATION,
                            event.position.event_seq,
                        )
                case _:
                    if (said := project_notice(event, conversation_id=conversation_id, room_id=room_id)) is not None:
                        await self._deliver(
                            room_id, attachment_id, conversation_id, said.body, said.kind, said.source_event_seq
                        )
            for close in closes:
                match close:
                    case SealSpan(span=span, body=body):
                        # Sealed like a projected notice: awaited, so the cursor stays behind a
                        # scrollback fact the homeserver has not accepted.
                        await self._frontend.seal_span(room_id, attachment_id, span, body)
                    case RetireSpan(span=span):
                        await self._frontend.retire_span(room_id, attachment_id, span)
        await state.reconcile(self._frontend, room_id, attachment_id, now=self._clock())
        await self._sweep_once(state, room_id, attachment_id)
        await subscription.keep(read.position)
        state.prune(read.position)
        return read.more

    async def _rebuilt(self, conversation_id: UUID, through: StreamPosition) -> LiveSpans:
        """The span fold for *conversation_id*, folded at least to *through* — the cursor.

        Rebuilt from the log's start once per subscriber, without rendering anything: the fold's
        output while catching up is present-tense state, and history is what the cursor already
        covered. A fold this subscriber already holds is only folded *forward* — it can sit
        ahead of the cursor after a pass whose keep failed, and never behind it except when the
        cursor moved without a batch (an `Unstarted` read taking the head), where the gap is
        history by the same rule.
        """
        state = self._state
        if state is None:
            state = LiveSpans(conversation_id)
            self._state = state
            self._swept = False
        position = state.folded_through
        while position < through:
            batch = await self._stream.read(conversation_id, after=position)
            events = tuple(event for event in batch.events if event.position <= through)
            if not events:
                break
            for event in events:
                state.advance(event)
            position = events[-1].position
        state.prune(through)
        return state

    async def _sweep_once(self, state: LiveSpans, room_id: str, attachment_id: UUID) -> None:
        """Retire span lines nothing open accounts for, once per rebuilt fold.

        The repairs this covers are the ones no replay reaches: a retirement lost with its replica
        (the redact is best-effort), and a line whose subject vocabulary this release no longer
        writes at all.
        """
        if self._swept:
            return
        await self._frontend.retire_stale_spans(room_id, attachment_id, state.open_subjects())
        self._swept = True

    async def _deliver(
        self,
        room_id: str,
        attachment_id: UUID,
        conversation_id: UUID,
        body: str,
        kind: RoomEventKind,
        source_event_seq: int,
    ) -> None:
        """Project one notice under its durable source, unless the room already shows it.

        Correspondence first, then send: a source the room already shows — found via its own tag —
        is a replay of a send that succeeded before the cursor could record it, and re-sending it
        is exactly the duplicate Synapse's expired transaction cache would no longer refuse.
        """
        if await self._room_copy.shows(attachment_id, source_event_seq):
            logger.info(
                "Matrix: %s already shows event %d of %s; not sending it again",
                room_id,
                source_event_seq,
                conversation_id,
            )
            return
        await self._project(room_id, attachment_id, body, kind, conversation_id, source_event_seq)

    async def _silent(self, turn_id: UUID) -> bool:
        """Whether *turn_id* ended answered without completing a non-empty assistant message."""
        async with self._sessions() as db:
            said = int(
                await db.scalar(
                    select(func.count(ConversationItem.item_id)).where(
                        ConversationItem.turn_id == turn_id,
                        ConversationItem.item_type == ItemType.MESSAGE,
                        ConversationItem.status == ItemStatus.COMPLETE,
                        func.trim(ConversationItem.item_text) != "",
                    )
                )
                or 0
            )
        return said == 0

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
            return None if _arrived_here(item.origin, room_id) else RELAYED_PROMPT + item.item_text

    def _wake(self, change: ConversationWakeEvent | RecheckHeld) -> None:
        """Note that this subscriber's conversation may have moved. Runs on the listener's reader
        task: no awaiting.

        A wake naming another conversation is a sibling room's and is dropped; `RecheckHeld` names
        nothing and every subscriber looks, because the notifications a reconnect lost are gone.
        """
        match change:
            case ConversationWakeEvent(conversation_id=conversation_id):
                if conversation_id != self._binding.conversation_id:
                    return
            case RecheckHeld():
                pass
        self._changed.set()

    async def _run(self) -> None:
        """Reconcile until cancelled."""
        while True:
            try:
                # Cleared before the pass, so a change committed while it runs wakes the next one
                # instead of being cleared away after it was already missed.
                self._changed.clear()
                if not await self.reconcile_once():
                    state = self._state
                    delay = (state.tick_seconds if state is not None else None) or POLL_INTERVAL.total_seconds()
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(delay):
                            await self._changed.wait()
            except Exception:
                logger.exception("Matrix: reconciling the notices of %s failed", self._binding.room_id)
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        with self._notifications.watch(self._wake):
            task = asyncio.create_task(self._run(), name=f"matrix-room-notices-{self._binding.attachment_id}")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
