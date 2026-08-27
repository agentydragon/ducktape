"""One conversation's read entries, built from the materialised rows rather than a fold.

<conversation_records.py> is what `read_items` hands back; this module is how one materialised row
becomes one of those entries. The rows are `conversation_item` and `conversation_turn` — themselves
folds of the log, asserted so by <reprojection.py> — which is what lets a page be served by keyset
reads instead of refolding the conversation from its first row: an entry needs nothing the row and
its one defining `conversation_event` row do not carry.

**An entry is defined by exactly one stream position.** A tool call's entry is written where the
call opens — its arguments are whole by then — and every other item's entry where the item
completes, because an item that never completed is not an entry: a turn that died mid-message left
prose nothing finished saying. A turn's end is its own entry at the `turn_ended` row. Those
positions are `opened_seq`, `closed_seq` and `last_seq` on the rows, so the store pages on them.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from haku.console.chat_models import EventProvenance, ItemType, PromptOrigin, ReasoningDisclosure, TurnOutcome

# The ORM row and the neutral vocabulary's union share a name; the row is aliased so a reader can
# tell them apart, as `session_store` does.
from haku.console.database_schema import ConversationEvent as ConversationEventRow, ConversationItem, ConversationTurn
from haku.console.x.conversation_records import (
    ConsoleAuthored,
    ConversationEntry,
    EntryProvenance,
    FromFrames,
    MessageEntry,
    Outcome,
    PromptEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TurnAbortedEnd,
    TurnAnsweredEnd,
    TurnEnd,
    TurnEndEntry,
    TurnFailedEnd,
)

_PROMPT_ORIGIN = TypeAdapter[PromptOrigin](PromptOrigin)


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
