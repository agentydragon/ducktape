"""One conversation's read entries, built from the materialised rows rather than a fold.

<conversation_reads.py> is what the conversation reads hand back; this module is how one
materialised row becomes one of those shapes — the single fold both surfaces consume. An entry is
the wire shape of one conversation item — or of a turn's boundary — as a read serves it: neither
the ORM row nor a stream event, but the folded item with its prose whole and its provenance
attached. The rows are `conversation_item` and `conversation_turn` — themselves folds of the log,
asserted so by <reprojection.py> — which is what lets a page be served by keyset reads instead of
refolding the conversation from its first row: an entry needs nothing the row and its one defining
`conversation_event` row do not carry.

**An entry is defined by exactly one stream position.** A tool call's entry is written where the
call opens — its arguments are whole by then — and every other item's entry where the item
completes, because an item that never completed is not an entry: a turn that died mid-message left
prose nothing finished saying. A turn's two boundaries are their own entries at its `turn_started`
and `turn_ended` rows. Those positions are `opened_seq`, `closed_seq`, `first_seq` and `last_seq`
on the rows, so the store pages on them.

**Two projections of the one fold.** `entry_of` serves the settled stream the MCP read returns;
`view_entry_of` extends it with the members only the SPA carries — turn starts, and the
cut-off prose a dead session left at its `closed_seq` (stamped to `opened_seq`, the one position
such an item has). The store produces the rows for both; which rows a read asks for is the read's
own contract (`SessionStore.read_item_rows` versus `read_conversation_view_rows`).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter

from haku.console.chat_models import EventProvenance, ItemType, PromptOrigin, ReasoningDisclosure, TurnOutcome

# The ORM row and the neutral vocabulary's union share a name; the row is aliased so a reader can
# tell them apart, as `session_store` does.
from haku.console.database_schema import ConversationEvent as ConversationEventRow, ConversationItem, ConversationTurn
from haku.console.x.conversation_reads import (
    ConsoleAuthored,
    ConversationEntry,
    ConversationViewEntry,
    CutOffItemEntry,
    EntryProvenance,
    FromFrames,
    MessageEntry,
    Outcome,
    PromptEntry,
    ReasoningEntry,
    StreamingItem,
    ToolCallEntry,
    ToolResultEntry,
    TurnAbortedEnd,
    TurnAnsweredEnd,
    TurnEnd,
    TurnEndEntry,
    TurnFailedEnd,
    TurnStartedEntry,
)

_PROMPT_ORIGIN = TypeAdapter[PromptOrigin](PromptOrigin)

# The prose types a turn streams into, and so the only types an open or cut-off item can carry to
# a reader. A tool call is settled at its opening instead, and a prompt is closed in one breath.
PROSE_ITEM_TYPES = (ItemType.MESSAGE, ItemType.REASONING)


@dataclass(frozen=True, slots=True)
class OpenedCall:
    """A tool call at its opening row — the one entry written before its item completes."""

    item: ConversationItem
    defining: ConversationEventRow


@dataclass(frozen=True, slots=True)
class CompletedItem:
    """Any completed item at its closing row."""

    item: ConversationItem
    defining: ConversationEventRow


@dataclass(frozen=True, slots=True)
class EndedTurn:
    """An ended turn at its `turn_ended` row."""

    turn: ConversationTurn


@dataclass(frozen=True, slots=True)
class StartedTurn:
    """A turn at its `turn_started` row. Only the conversation-view read serves these."""

    turn: ConversationTurn


@dataclass(frozen=True, slots=True)
class CutOffItem:
    """A prose item a dead session left unfinished, at the opening row failing it closed it on.

    Only the conversation-view read serves these; *defining* is that opening row, for its provenance.
    """

    item: ConversationItem
    defining: ConversationEventRow


type ConversationPageRow = OpenedCall | CompletedItem | EndedTurn

type ConversationViewPageRow = ConversationPageRow | StartedTurn | CutOffItem


def turn_end_of(turn: ConversationTurn) -> TurnEnd | None:
    """How *turn* ended, or None while it is still running.

    `ck_conversation_turn_failure` is what makes the failed arm's reason always present.
    """
    match turn.outcome:
        case None:
            return None
        case TurnOutcome.ANSWERED:
            return TurnAnsweredEnd()
        case TurnOutcome.ABORTED:
            return TurnAbortedEnd()
        case TurnOutcome.FAILED:
            assert turn.failure is not None, "ck_conversation_turn_failure"
            return TurnFailedEnd(failure=turn.failure)


def provenance_of(event: ConversationEventRow) -> EntryProvenance:
    """The union the row's provenance column and its frame range spell out together.

    A frame-derived row names its session (`ck_conversation_event_provenance_frames`), and the
    entry carries it: the conversation's entries span replaced sessions, so a frame number alone
    would not say whose wire log to appeal to.
    """
    match event.provenance, event.source_first_frame_seq, event.source_last_frame_seq:
        case EventProvenance.FRAME_RANGE, int(first), int(last):
            if event.session_id is None:
                raise ValueError(f"a frame-derived row names no session: {event.event_seq=}")
            return FromFrames(session_id=event.session_id, first_frame_seq=first, last_frame_seq=last)
        case EventProvenance.AUTHORED, None, None:
            return ConsoleAuthored()
        case _:
            raise ValueError(f"a row's provenance and its frame range disagree: {event.event_seq=}")


def opened_entry(item: ConversationItem, event: ConversationEventRow) -> ToolCallEntry:
    """The entry a tool call's opening row writes; *event* is that row, for its provenance."""
    if item.item_type is not ItemType.TOOL_CALL or item.call_id is None or item.tool_name is None:
        raise ValueError(f"only a tool call has an entry at its opening: {item.item_id=} {item.item_type=}")
    return ToolCallEntry(
        seq=item.opened_seq,
        provenance=provenance_of(event),
        call_id=item.call_id,
        tool_name=item.tool_name,
        arguments=dict(item.arguments) if item.arguments is not None else {},
    )


def completed_entry(item: ConversationItem, event: ConversationEventRow) -> ConversationEntry:
    """The entry a completed item's closing row writes; *event* is that row, for its provenance."""
    if item.closed_seq is None:
        raise ValueError(f"an open item has no completion entry: {item.item_id=}")
    seq, provenance = item.closed_seq, provenance_of(event)
    match item.item_type:
        case ItemType.PROMPT:
            if item.origin is None:
                raise ValueError(f"a prompt row names no origin: {item.item_id=}")
            return PromptEntry(
                seq=seq,
                provenance=provenance,
                text=item.item_text,
                origin=_PROMPT_ORIGIN.validate_python(item.origin).kind,
            )
        case ItemType.MESSAGE:
            return MessageEntry(
                seq=seq, provenance=provenance, text=item.item_text, backend_item_id=item.backend_item_id
            )
        case ItemType.REASONING:
            withheld = item.disclosure is ReasoningDisclosure.WITHHELD
            return ReasoningEntry(seq=seq, provenance=provenance, summary=None if withheld else item.item_text)
        case ItemType.TOOL_CALL:
            assert item.call_id is not None, "ck_conversation_item_tool_call_fields"
            assert item.outcome is not None, "ck_conversation_item_complete_terminal_fields"
            return ToolResultEntry(
                seq=seq,
                provenance=provenance,
                call_id=item.call_id,
                content=item.item_text,
                structured=item.structured,
                outcome=Outcome(item.outcome),
            )


def turn_end_entry(turn: ConversationTurn) -> TurnEndEntry:
    """The entry an ended turn's `turn_ended` row writes.

    Authored provenance without reading the row back: a turn's two ends are the console's own
    account, written by `end_turn`/`complete_frame` as authored rows whatever frame the exchange
    ended on.
    """
    end = turn_end_of(turn)
    if end is None or turn.last_seq is None:
        raise ValueError(f"a running turn has no end entry: {turn.turn_id=}")
    return TurnEndEntry(seq=turn.last_seq, provenance=ConsoleAuthored(), end=end)


def turn_started_entry(turn: ConversationTurn) -> TurnStartedEntry:
    """The entry a turn's `turn_started` row writes — authored, like the end that pairs with it."""
    return TurnStartedEntry(seq=turn.first_seq, provenance=ConsoleAuthored())


def cut_off_entry(item: ConversationItem, event: ConversationEventRow) -> CutOffItemEntry:
    """The entry a failed prose item's closing writes; *event* is its opening row, for provenance.

    The opening row rather than a closing one, because failing a session stamps `closed_seq` to
    `opened_seq` and writes nothing new: the one position such an item has is where it began.
    """
    if item.item_type not in PROSE_ITEM_TYPES or item.closed_seq is None:
        raise ValueError(f"only failed prose has a cut-off entry: {item.item_id=} {item.item_type=}")
    return CutOffItemEntry(
        seq=item.closed_seq, provenance=provenance_of(event), item_type=item.item_type, text=item.item_text
    )


def streaming_item(item: ConversationItem) -> StreamingItem:
    """A still-open prose item as the live tail carries it."""
    if item.item_type not in PROSE_ITEM_TYPES:
        raise ValueError(f"only open prose streams: {item.item_id=} {item.item_type=}")
    return StreamingItem(item_type=item.item_type, text=item.item_text)


def entry_of(row: ConversationPageRow) -> ConversationEntry:
    """The wire entry one settled page row folds to."""
    match row:
        case OpenedCall(item=item, defining=defining):
            return opened_entry(item, defining)
        case CompletedItem(item=item, defining=defining):
            return completed_entry(item, defining)
        case EndedTurn(turn=turn):
            return turn_end_entry(turn)


def view_entry_of(row: ConversationViewPageRow) -> ConversationViewEntry:
    """The wire entry one conversation-view page row folds to — the SPA's superset of `entry_of`."""
    match row:
        case StartedTurn(turn=turn):
            return turn_started_entry(turn)
        case CutOffItem(item=item, defining=defining):
            return cut_off_entry(item, defining)
        case _:
            return entry_of(row)
