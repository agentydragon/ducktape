"""The neutral conversation vocabulary as `haku_conversations` hands it out.

<session_events.py> is what a conversation *is* once it is stored, in bodies the console's own code
folds; <conversation_records.py> is what a read hands back, in Pydantic models
<../tools/conversations.py> serialises. This is the one place the two are the same conversation, so
a change to either shows up here rather than as a surface quietly drifting from the vocabulary it
claims to expose.

**The log is what is folded, not the frames behind it.** `conversation_event` is the record, written
once as each frame arrived, so a transcript says what the console recorded rather than what today's
adapter would now make of the same frames. This fold therefore names no harness, and a session that
ran before a projection fix keeps the history it had — <reprojection.py> says where the two differ.

**Segments are folded here rather than handed on.** The vocabulary emits prose as increments so a
live channel can print them as they arrive; a transcript is read after the fact and wants the item,
whose text is the concatenation of exactly those increments.

**An item that never completed is not an entry.** A turn that died mid-message left prose nothing
finished saying, and a transcript that printed it would report a half-sentence as what was said.

**What remains is numbered.** The index is the entry's position in the whole session's transcript
and is what `read_transcript`'s cursor names; see `TranscriptCursor` for why an ordinal is a safe
key for this one order.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

from haku.console.chat_models import EventProvenance, ReasoningDisclosure
from haku.console.database_schema import ConversationEvent as ConversationEventRow
from haku.console.x import conversation_records, session_events

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Open:
    """An item the fold has seen the start of: what opening it said, and the prose since.

    The opening body is kept because a completion does not repeat it: a call's `call_id` and a
    prompt's origin are written once, where the item opens, and the log pairs an item's two ends by
    `item_id`.
    """

    started: session_events.ItemStartedBody
    segments: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(self.segments)


@dataclass(frozen=True, slots=True)
class Transcript:
    """One session's whole transcript, and what its log held that this release cannot read."""

    entries: list[conversation_records.TranscriptEntry]
    unreadable: dict[str, int] | None


def fold(rows: Iterable[ConversationEventRow]) -> Transcript:
    """One session's stored log as the wire models: one entry per finished item, then numbered.

    *rows* are that session's `conversation_event` rows in `event_seq` order, which is the order
    they were written in and so the order the entries come out in.
    """
    open_items: dict[UUID, _Open] = {}
    said: list[conversation_records.TranscriptEntry] = []
    unread: Counter[str] = Counter()
    for row in rows:
        body = session_events.body_of(row)
        if isinstance(body, session_events.UnknownEventBody):
            # A row a newer release wrote. Counted rather than skipped, because a transcript that is
            # quietly missing something is worse than one that says what it missed.
            unread[body.kind] += 1
        elif (entry := _entry(row, body, open_items, index=len(said))) is not None:
            said.append(entry)
    return Transcript(entries=said, unreadable=dict(unread) or None)


def _entry(
    row: ConversationEventRow, body: session_events.WrittenBody, open_items: dict[UUID, _Open], *, index: int
) -> conversation_records.TranscriptEntry | None:
    """This row's entry, where it produces one, folding what it says into the item it names."""
    match body:
        case session_events.ToolCallStartedBody():
            open_items[_item(row)] = _Open(started=body)
            # The one item whose entry is written at its start: its arguments are whole by then, and
            # its answer is a separate entry joined by `call_id`.
            return conversation_records.ToolCallEntry(
                index=index,
                provenance=_provenance(row),
                call_id=body.call_id,
                tool_name=body.tool_name,
                arguments=dict(body.arguments),
            )
        case (
            session_events.MessageStartedBody()
            | session_events.ReasoningStartedBody()
            | session_events.PromptStartedBody()
        ):
            # Never reopens: a fold resuming mid-message writes a second opening row about the
            # message its predecessor left open (<conversation_log.py> `_item_for`), and starting
            # fresh here would drop the prose already folded into it.
            open_items.setdefault(_item(row), _Open(started=body))
            return None
        case session_events.SegmentBody():
            if (opened := open_items.get(_item(row))) is None:
                _unopened(row)
                return None
            opened.segments.append(body.text)
            return None
        case (
            session_events.MessageCompletedBody()
            | session_events.ReasoningCompletedBody()
            | session_events.ToolCallCompletedBody()
            | session_events.PromptCompletedBody()
        ):
            if (opened := open_items.pop(_item(row), None)) is None:
                _unopened(row)
                return None
            return _finished(opened, body, index=index, provenance=_provenance(row))
        case session_events.TurnAnsweredBody() | session_events.TurnAbortedBody() | session_events.TurnFailedBody():
            return conversation_records.TurnEndEntry(index=index, provenance=_provenance(row), end=_end(body))
        case (
            session_events.TurnStartedBody()
            | session_events.PromptRejectedBody()
            | session_events.UnreadableInputBody()
            | session_events.SessionAdoptedBody()
            | session_events.LeaseExpiredBody()
            | session_events.SessionProvisioningBody()
            | session_events.SessionEndedBody()
            | session_events.SetupNarrationBody()
        ):
            # The session's own story rather than the conversation's — which replica held the
            # lease, what the sandbox printed, a prompt refused and never delivered. The console's
            # session views are where those are read.
            return None


def _finished(
    opened: _Open,
    body: session_events.ItemCompletedBody,
    *,
    index: int,
    provenance: conversation_records.EntryProvenance,
) -> conversation_records.TranscriptEntry:
    """One finished item as its entry: what its two ends said, and the prose between them."""
    match body, opened.started:
        case session_events.MessageCompletedBody(), _:
            return conversation_records.MessageEntry(
                index=index, provenance=provenance, text=opened.text(), backend_item_id=body.backend_item_id
            )
        case session_events.ReasoningCompletedBody(), _:
            withheld = body.disclosure is ReasoningDisclosure.WITHHELD
            return conversation_records.ReasoningEntry(
                index=index, provenance=provenance, summary=None if withheld else opened.text()
            )
        case session_events.ToolCallCompletedBody(), session_events.ToolCallStartedBody(call_id=call_id):
            return conversation_records.ToolResultEntry(
                index=index,
                provenance=provenance,
                call_id=call_id,
                content=opened.text(),
                structured=body.structured,
                outcome=conversation_records.Outcome(body.outcome),
            )
        case session_events.PromptCompletedBody(), session_events.PromptStartedBody(origin=origin):
            return conversation_records.PromptEntry(
                index=index, provenance=provenance, text=opened.text(), origin=origin.kind
            )
        case _:
            raise ValueError(f"an item's two ends name different types: {body=} {opened.started=}")


def _end(body: session_events.TurnEndedBody) -> conversation_records.TurnEnd:
    match body:
        case session_events.TurnAnsweredBody():
            return conversation_records.TurnAnsweredEnd()
        case session_events.TurnAbortedBody():
            return conversation_records.TurnAbortedEnd()
        case session_events.TurnFailedBody():
            return conversation_records.TurnFailedEnd(failure=body.failure)


def _unopened(row: ConversationEventRow) -> None:
    """A row about an item the fold does not have open.

    A `tool_result` block the wire repeated reaches the log as a second close, because the adapters
    address a call by its protocol id and do not deduplicate; the first close is the one that
    happened, as it is for a turn. Anything else means the log contradicts itself, which
    `conversation_log` refuses to write. Neither is worth losing the conversation over.
    """
    logger.warning("a transcript fold has no open item for a row about one: %s/%s", row.conversation_id, row.event_seq)


def _item(row: ConversationEventRow) -> UUID:
    """The item an item-kind row is about, which the table's own constraint says it names."""
    if row.item_id is None:
        raise ValueError(f"an item's row names no item: {row.conversation_id=} {row.event_seq=}")
    return row.item_id


def _provenance(row: ConversationEventRow) -> conversation_records.EntryProvenance:
    """The union the row's provenance column and its frame range spell out together."""
    match row.provenance, row.source_first_frame_seq, row.source_last_frame_seq:
        case EventProvenance.FRAME_RANGE, int(first), int(last):
            return conversation_records.FromFrames(first_frame_seq=first, last_frame_seq=last)
        case EventProvenance.AUTHORED, None, None:
            return conversation_records.ConsoleAuthored()
        case _:
            raise ValueError(f"a row's provenance and its frame range disagree: {row.event_seq=}")
