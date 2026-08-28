"""One conversation's read entries: the materialised item rows, minimally wrapped.

<conversation_reads.py> is what the conversation reads hand back; this module is how one
`conversation_item` row becomes one of those entries — the single fold every reader consumes. An
entry is the row with its wire face on: the row's `item_type` as the entry's kind, the row's
lifecycle as its `status`, the prose as it stands, and the frame span its content was read off as
`provenance`. The rows are folds of the log the writer keeps — asserted so by <reprojection.py> —
which is what lets a page be served by a keyset read instead of refolding the conversation from
its first row.

**An entry sits at the row's opening position for its whole life.** `opened_seq` is allocated when
the item opens and never moves, so paging is stable while an item is still being written: a later
read serves the same position with the row's newer state — more prose, an answer, a settled
`status` — never a second entry somewhere else. What a page holds is therefore the rows as of the
read, and re-reading any position is always correct.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter

from haku.console.chat_models import ItemType, PromptOrigin, TurnOutcome
from haku.console.conversation.reads import (
    ConsoleAuthored,
    ConversationEntry,
    EntryProvenance,
    FromFrames,
    MessageEntry,
    Outcome,
    PromptEntry,
    ReasoningEntry,
    ToolCallEntry,
    TurnAbortedEnd,
    TurnAnsweredEnd,
    TurnEnd,
    TurnFailedEnd,
)
from haku.console.database_schema import ConversationItem, ConversationTurn

_PROMPT_ORIGIN = TypeAdapter[PromptOrigin](PromptOrigin)


@dataclass(frozen=True, slots=True)
class ConversationPageRow:
    """One item row of a page, with the frame span its events were read off.

    The span is aggregated from the row's `conversation_event` rows — absent for a row whose every
    event was console-authored, a prompt being the case that exists.
    """

    item: ConversationItem
    first_frame_seq: int | None
    last_frame_seq: int | None


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


def _provenance_of(row: ConversationPageRow) -> EntryProvenance:
    """The frame span the row's content was read off, or the console's own authorship.

    A frame-derived row names its session (`ck_conversation_event_provenance_frames` holds it on
    every event in the span), and the entry carries it: a conversation's entries span replaced
    sessions, so a frame number alone would not say whose wire log to appeal to.
    """
    match row.first_frame_seq, row.last_frame_seq:
        case None, None:
            return ConsoleAuthored()
        case int(first), int(last):
            if row.item.session_id is None:
                raise ValueError(f"a frame-derived row names no session: {row.item.item_id=}")
            return FromFrames(session_id=row.item.session_id, first_frame_seq=first, last_frame_seq=last)
        case _:
            raise ValueError(f"a row's frame span has one end: {row.item.item_id=}")


def entry_of(row: ConversationPageRow) -> ConversationEntry:
    """The wire entry one item row folds to."""
    item = row.item
    provenance = _provenance_of(row)
    match item.item_type:
        case ItemType.PROMPT:
            if item.origin is None:
                raise ValueError(f"a prompt row names no origin: {item.item_id=}")
            return PromptEntry(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                origin=_PROMPT_ORIGIN.validate_python(item.origin).kind,
            )
        case ItemType.MESSAGE:
            return MessageEntry(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                backend_item_id=item.backend_item_id,
            )
        case ItemType.REASONING:
            return ReasoningEntry(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                disclosure=item.disclosure,
            )
        case ItemType.TOOL_CALL:
            assert item.call_id is not None, "ck_conversation_item_tool_call_fields"
            assert item.tool_name is not None, "ck_conversation_item_tool_call_fields"
            return ToolCallEntry(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                call_id=item.call_id,
                tool_name=item.tool_name,
                arguments=dict(item.arguments) if item.arguments is not None else {},
                content=item.item_text,
                structured=item.structured,
                outcome=Outcome(item.outcome) if item.outcome is not None else None,
            )
