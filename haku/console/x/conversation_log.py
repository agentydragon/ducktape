"""Appending to one conversation's log, and materialising the items it touches.

The only writer of `conversation_event`, `conversation_item` and `conversation_turn`. Everything
else that used to write a transcript row — the turn loop's own message bookkeeping, the paths that
minted prose no log row stood behind — goes through here or does not happen.

**The log is written first and the entities follow from it, in one transaction.** That is the whole
point of the shape: an item's `text` is the concatenation of its segments, so a reader can check it
rather than trust it, and there is no second authority to disagree with the first
(<../docs/conversation_schema.md> § 2).

**The caller holds the conversation row locked.** `event_seq` is handed out from
`conversation.next_event_seq`, which must be taken `FOR UPDATE` before a writer is built: the
address is dense, so two writers may not allocate concurrently. That costs one row lock per write
and is affordable — segments are coalesced, so a turn writes tens of rows rather than thousands,
and only one session holds a conversation at a time.

**Two ways an item is addressed, and neither is a key the fold invented.** A tool call answered
frames after it was asked is found by `call_id` — the id the protocol supplies for exactly that
purpose. Everything else is "the open item of this type", answered from this transaction's own
opens and otherwise from the turn's open items in the database, so a fold resuming mid-item finds
what its predecessor left open. Identity is the `item_id` minted here and nowhere else.

**The neutral-operation journal (#4667) addresses differently, through the `runner_*` methods:**
every operation names the runner's own stable id for its item, stored as
`conversation_item.runner_item_id`, so concurrent open items of one type are expressible and no
fold state is consulted. Identity is still minted here; the runner's id is how a later operation
finds the row, never what the row is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import (
    ConversationEventKind,
    ItemStatus,
    ItemType,
    PromptOrigin,
    ReasoningDisclosure,
    ToolOutcome,
)
from haku.console.database_schema import Conversation, ConversationItem, ConversationTurn
from haku.console.x import session_events
from haku.console.x.conversation_events import (
    CallRef,
    ConversationEvent,
    FrameRange,
    ItemRef,
    ItemSegment,
    Json,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from haku.runtime.x.bridge import neutral_operations


class UnknownItemError(LookupError):
    """An event named an item the log has no record of opening.

    A defect in an adapter or a fold, never something a backend can cause: a completion whose start
    was never projected means the two halves disagree about what happened, and writing the second
    half alone would leave prose attached to nothing.
    """


def _range(provenance: neutral_operations.FrameRange) -> FrameRange:
    """The journal's frame span in the record vocabulary's spelling.

    Two classes because the import direction forbids one: the wire cannot import the Console's
    record layer, so each side spells the same two integers itself.
    """
    return FrameRange(first_frame_seq=provenance.first_frame_seq, last_frame_seq=provenance.last_frame_seq)


@dataclass(slots=True)
class LogWriter:
    """One transaction's worth of appends to a conversation's log.

    Built per transaction, never held across one: it caches the item ids it minted, which are only
    valid inside the transaction that wrote them.
    """

    db: AsyncSession
    conversation: Conversation
    session_id: UUID | None
    turn_id: UUID | None
    now: datetime
    # What this transaction has opened and not yet closed, per type. Consulted before the database,
    # because an item opened a moment ago in this same transaction is not visible to a query until
    # it flushes and is the common case besides.
    _open_of_type: dict[ItemType, UUID] = field(default_factory=dict)

    def _next_seq(self) -> int:
        """The next position, taken from the counter the caller locked."""
        allocated = self.conversation.next_event_seq
        self.conversation.next_event_seq = allocated + 1
        return allocated

    async def append(self, event: ConversationEvent) -> None:
        """Write one neutral event: its log row, and what it makes of the item it names.

        A `TurnCompleted` writes nothing here — the turn's own row holds the outcome and the log
        states the two ends as authored rows, which `end_turn` writes.
        """
        if isinstance(event, TurnCompleted):
            return
        if (stored := session_events.stored(event)) is None:
            return
        kind, body = stored
        if not isinstance(frames := event.provenance, FrameRange):
            raise ValueError(f"{kind} is projected from frames and names none: {event=}")
        item_id = await self._item_for(event, frames)
        self.db.add(
            session_events.item_row(
                kind,
                body,
                conversation_id=self.conversation.conversation_id,
                event_seq=self._next_seq(),
                item_id=item_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                provenance=frames,
                now=self.now,
            )
        )

    async def _item_for(self, event: ConversationEvent, frames: FrameRange) -> UUID:
        """The item this event is about, opening one where the event is what opens it."""
        match event:
            case MessageStarted():
                # **Continues the turn's open message where there is one.** A fold resuming from a
                # cursor mid-message was seeded empty, so it says "the agent began saying
                # something" about a message its predecessor had already opened; taking that
                # literally would leave two open items and split one answer in two. What decides is
                # the row, because the row is what survived the replica that wrote it.
                if (resumed := await self._resume(ItemType.MESSAGE)) is not None:
                    return resumed
                return await self._open(ItemType.MESSAGE)
            case ReasoningStarted():
                return await self._open(ItemType.REASONING)
            case ToolCallStarted():
                return await self._open(
                    ItemType.TOOL_CALL,
                    call_id=event.call_id,
                    tool_name=event.tool_name,
                    arguments=dict(event.arguments),
                )
            case ItemSegment():
                item = await self._item(await self._resolve(event.item))
                item.item_text += event.text
                item.updated_at = self.now
                return item.item_id
            case MessageCompleted():
                item = await self._close(await self._resolve(OpenRef(item_type=ItemType.MESSAGE)))
                item.backend_item_id = event.backend_item_id
                return item.item_id
            case ReasoningCompleted():
                item = await self._close(await self._resolve(OpenRef(item_type=ItemType.REASONING)))
                item.disclosure = event.disclosure
                return item.item_id
            case ToolCallCompleted():
                item = await self._close(await self._resolve(event.item))
                item.outcome = event.outcome
                item.structured = event.structured
                return item.item_id
            case TurnCompleted():
                raise AssertionError("a turn's end names no item")

    async def _open(
        self,
        item_type: ItemType,
        *,
        call_id: str | None = None,
        tool_name: str | None = None,
        arguments: dict[str, Json] | None = None,
    ) -> UUID:
        item_id = uuid4()
        self.db.add(
            ConversationItem(
                item_id=item_id,
                conversation_id=self.conversation.conversation_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                item_type=item_type,
                status=ItemStatus.OPEN,
                # The position of the row about to be written for it. Allocated here rather than
                # read back, so the item and its opening event cannot disagree about where it began.
                opened_seq=self.conversation.next_event_seq,
                closed_seq=None,
                item_text="",
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        # Flushed before anything points at it: `conversation_event.item_id` is a foreign key.
        await self.db.flush()
        self._open_of_type[item_type] = item_id
        return item_id

    async def _resume(self, item_type: ItemType) -> UUID | None:
        """The item of this type this writer or the turn already has open, if either does."""
        if (opened := self._open_of_type.get(item_type)) is not None:
            return opened
        found: UUID | None = await self.db.scalar(
            select(ConversationItem.item_id).where(
                ConversationItem.turn_id == self.turn_id,
                ConversationItem.item_type == item_type,
                ConversationItem.status == ItemStatus.OPEN,
            )
        )
        if found is not None:
            self._open_of_type[item_type] = found
        return found

    async def _resolve(self, ref: ItemRef) -> UUID:
        match ref:
            case CallRef():
                found = await self.db.scalar(
                    select(ConversationItem.item_id).where(
                        ConversationItem.conversation_id == self.conversation.conversation_id,
                        ConversationItem.call_id == ref.call_id,
                    )
                )
                if found is None:
                    raise UnknownItemError(f"no call was asked under this id: {ref.call_id=}")
                return found
            case OpenRef():
                # What replaces the pointer the turn used to carry: the state is the item's, so
                # there is one place it can be wrong rather than two that can disagree. One answer,
                # because a backend writes one item of a type at a time.
                if (open_item := await self._resume(ref.item_type)) is None:
                    raise UnknownItemError(
                        f"no item of this type is open on the turn: {ref.item_type=} {self.turn_id=}"
                    )
                return open_item

    async def _item(self, item_id: UUID) -> ConversationItem:
        """The row an event names.

        Segments append to `item_text` here rather than being re-joined on read, and
        `reprojection` asserts the two agree — the check the old shape could not make, because its
        transcript row and its log were written from different places.
        """
        item = await self.db.get(ConversationItem, item_id)
        if item is None:
            raise UnknownItemError(f"an event names an item that does not exist: {item_id=}")
        return item

    async def _close(self, item_id: UUID) -> ConversationItem:
        """Move an item to its terminal state. The caller sets the fields its type owns."""
        item = await self._item(item_id)
        item.status = ItemStatus.COMPLETE
        item.closed_seq = self.conversation.next_event_seq
        item.updated_at = self.now
        self._open_of_type.pop(item.item_type, None)
        return item

    async def runner_opened(self, opened: neutral_operations.ItemOpened, *, turn_id: UUID) -> UUID:
        """One neutral-journal item opens: its row, and its `item_started` row.

        The runner arm of `_open`. Identity is still minted here — the runner's `item_id` is the
        *addressing* key later operations name (`uq_conversation_item_runner`), never this table's.
        A tool call's `call_id` is that same runner id: the neutral journal supplies it as the
        correlation id for call and answer (`item.completed` names it), which is exactly what the
        column and `uq_conversation_item_call` exist for.

        *turn_id* is required where the wire's is optional: `conversation_event` pins every
        frame-derived row to a turn (`ck_conversation_event_provenance_frames`), so the caller
        rejects an item observed outside any turn before it gets here.
        """
        call_id: str | None = None
        tool_name: str | None = None
        arguments: dict[str, Any] | None = None
        body: session_events.ItemStartedBody
        match opened.item:
            case neutral_operations.MessageOpen():
                item_type, body = ItemType.MESSAGE, session_events.MessageStartedBody()
            case neutral_operations.ReasoningOpen():
                item_type, body = ItemType.REASONING, session_events.ReasoningStartedBody()
            case neutral_operations.ToolCallOpen():
                item_type = ItemType.TOOL_CALL
                call_id, tool_name = str(opened.item_id), opened.item.tool_name
                arguments = dict(opened.item.arguments)
                body = session_events.ToolCallStartedBody(call_id=call_id, tool_name=tool_name, arguments=arguments)
        item_id = uuid4()
        self.db.add(
            ConversationItem(
                item_id=item_id,
                conversation_id=self.conversation.conversation_id,
                session_id=self.session_id,
                turn_id=turn_id,
                item_type=item_type,
                status=ItemStatus.OPEN,
                opened_seq=self.conversation.next_event_seq,
                closed_seq=None,
                item_text="",
                backend_item_id=opened.backend_item_id,
                runner_item_id=opened.item_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        await self.db.flush()
        self.db.add(
            session_events.item_row(
                ConversationEventKind.ITEM_STARTED,
                body,
                conversation_id=self.conversation.conversation_id,
                event_seq=self._next_seq(),
                item_id=item_id,
                session_id=self.session_id,
                turn_id=turn_id,
                provenance=_range(opened.provenance),
                now=self.now,
            )
        )
        return item_id

    async def runner_segment(self, segment: neutral_operations.ItemSegment) -> UUID:
        """A run of a neutral-journal item's prose: append it, and say so in the log."""
        item = await self._open_runner_item(segment.item_id)
        item.item_text += segment.text
        item.updated_at = self.now
        self.db.add(
            session_events.item_row(
                ConversationEventKind.ITEM_SEGMENT,
                session_events.SegmentBody(text=segment.text),
                conversation_id=self.conversation.conversation_id,
                event_seq=self._next_seq(),
                item_id=item.item_id,
                session_id=self.session_id,
                turn_id=item.turn_id,
                provenance=_range(segment.provenance),
                now=self.now,
            )
        )
        return item.item_id

    async def runner_completed(self, completed: neutral_operations.ItemCompleted) -> UUID:
        """A neutral-journal item's terminal operation: close it with the fields its type owns."""
        item = await self._open_runner_item(completed.item_id)
        completion = completed.completion
        if ItemType(completion.item_type.value) is not item.item_type:
            raise UnknownItemError(
                f"a completion's arm disagrees with the item it names:"
                f" {completion.item_type=} {item.item_type=} {completed.item_id=}"
            )
        body: session_events.ItemCompletedBody
        match completion:
            case neutral_operations.MessageCompletion():
                body = session_events.MessageCompletedBody(backend_item_id=completed.backend_item_id)
            case neutral_operations.ReasoningCompletion():
                item.disclosure = ReasoningDisclosure(completion.disclosure.value)
                body = session_events.ReasoningCompletedBody(disclosure=item.disclosure)
            case neutral_operations.ToolCallCompletion():
                item.outcome = ToolOutcome(completion.outcome.value)
                item.structured = completion.structured
                body = session_events.ToolCallCompletedBody(structured=completion.structured, outcome=item.outcome)
        if completed.backend_item_id is not None:
            # The open may already have named it; a protocol that names the item only when closing
            # it supplies it here. None never clears what the open recorded.
            item.backend_item_id = completed.backend_item_id
        await self._close(item.item_id)
        self.db.add(
            session_events.item_row(
                ConversationEventKind.ITEM_COMPLETED,
                body,
                conversation_id=self.conversation.conversation_id,
                event_seq=self._next_seq(),
                item_id=item.item_id,
                session_id=self.session_id,
                turn_id=item.turn_id,
                provenance=_range(completed.provenance),
                now=self.now,
            )
        )
        return item.item_id

    async def _open_runner_item(self, runner_item_id: UUID) -> ConversationItem:
        """The open item the runner's id names — the runner arm of `_resolve`.

        One query answers "never opened" and "already closed" alike: the journal's total order
        makes either a defect in the runner, handled the same way (the batch is rejected), so the
        distinction is not worth a second query.
        """
        assert self.session_id is not None, "runner items are session-scoped"
        item: ConversationItem | None = await self.db.scalar(
            select(ConversationItem).where(
                ConversationItem.session_id == self.session_id,
                ConversationItem.runner_item_id == runner_item_id,
                ConversationItem.status == ItemStatus.OPEN,
            )
        )
        if item is None:
            raise UnknownItemError(f"no open item under this runner id: {runner_item_id=}")
        return item

    async def authored_prompt(self, text: str, origin: PromptOrigin) -> UUID:
        """A prompt, as an item the console opened and closed in one breath.

        Authored on both rows: it is admitted before it crosses any wire, and a session that ends
        before claiming it never sends it at all. Its whole text is known at acceptance, so it has
        exactly one segment and no window in which it is open.
        """
        item_id = uuid4()
        self.db.add(
            ConversationItem(
                item_id=item_id,
                conversation_id=self.conversation.conversation_id,
                session_id=self.session_id,
                turn_id=None,
                item_type=ItemType.PROMPT,
                status=ItemStatus.COMPLETE,
                opened_seq=self.conversation.next_event_seq,
                closed_seq=self.conversation.next_event_seq + 2,
                item_text=text,
                origin=origin,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        await self.db.flush()
        for kind, body in (
            (ConversationEventKind.ITEM_STARTED, session_events.PromptStartedBody(origin=origin)),
            (ConversationEventKind.ITEM_SEGMENT, session_events.SegmentBody(text=text)),
            (ConversationEventKind.ITEM_COMPLETED, session_events.PromptCompletedBody()),
        ):
            self.db.add(
                session_events.item_row(
                    kind,
                    body,
                    conversation_id=self.conversation.conversation_id,
                    event_seq=self._next_seq(),
                    item_id=item_id,
                    session_id=self.session_id,
                    turn_id=None,
                    provenance=None,
                    now=self.now,
                )
            )
        return item_id

    def authored(self, body: session_events.AuthoredBody, *, turn_id: UUID | None = None) -> None:
        """One of the console's own facts, appended at the next position."""
        self.db.add(
            session_events.authored(
                body,
                conversation_id=self.conversation.conversation_id,
                event_seq=self._next_seq(),
                session_id=self.session_id,
                turn_id=turn_id,
                now=self.now,
            )
        )


async def writer_for(
    db: AsyncSession, conversation_id: UUID, *, session_id: UUID | None, turn_id: UUID | None, now: datetime
) -> LogWriter:
    """A writer over *conversation_id*, with its counter row locked for the caller's transaction."""
    conversation = await db.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None:
        raise KeyError(conversation_id)
    return LogWriter(db=db, conversation=conversation, session_id=session_id, turn_id=turn_id, now=now)


async def opened_turn(
    writer: LogWriter, *, runner_turn_id: UUID | None = None, first_frame_seq: int | None = None
) -> ConversationTurn:
    """Open an exchange and say so in the log, leaving the writer's ambient turn untouched.

    The shared trunk of `open_turn` and the journal consumer's `turn.opened`, which brackets many
    turns in one writer's transaction and so cannot use the ambient `turn_id`. The defaults are the
    old fold's valid omissions: it has no runner identity to record, and it anchors the frame bound
    itself after the row exists.
    """
    if writer.session_id is None:
        raise ValueError("a turn needs the session that runs it")
    turn = ConversationTurn(
        turn_id=uuid4(),
        conversation_id=writer.conversation.conversation_id,
        session_id=writer.session_id,
        first_seq=writer.conversation.next_event_seq,
        runner_turn_id=runner_turn_id,
        first_frame_seq=first_frame_seq,
        started_at=writer.now,
    )
    writer.db.add(turn)
    await writer.db.flush()
    writer.authored(session_events.TurnStartedBody(), turn_id=turn.turn_id)
    return turn


async def open_turn(
    db: AsyncSession, conversation_id: UUID, *, session_id: UUID, now: datetime
) -> tuple[ConversationTurn, LogWriter]:
    """Open an exchange and say so in the log, inside the caller's transaction."""
    writer = await writer_for(db, conversation_id, session_id=session_id, turn_id=None, now=now)
    turn = await opened_turn(writer)
    writer.turn_id = turn.turn_id
    return turn, writer
