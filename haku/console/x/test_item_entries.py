"""Tests for the row-to-entry mapping (`item_entries`)."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from haku.console.chat_models import (
    ConversationEventKind,
    EventProvenance,
    ItemStatus,
    ItemType,
    ReasoningDisclosure,
    ToolOutcome,
    TurnOutcome,
)
from haku.console.database_schema import ConversationEvent as ConversationEventRow, ConversationItem, ConversationTurn
from haku.console.x import item_entries
from haku.console.x.conversation_reads import (
    ConsoleAuthored,
    FromFrames,
    PromptEntry,
    ReasoningEntry,
    ToolResultEntry,
    TurnFailedEnd,
)

CONVERSATION = UUID("44444444-4444-4444-4444-444444444444")
SESSION = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)


def _item(
    item_type: ItemType,
    *,
    text: str = "",
    closed_seq: int | None = 5,
    origin: dict[str, Any] | None = None,
    call_id: str | None = None,
    tool_name: str | None = None,
    outcome: ToolOutcome | None = None,
    structured: Any | None = None,
    disclosure: ReasoningDisclosure | None = None,
) -> ConversationItem:
    return ConversationItem(
        item_id=uuid4(),
        conversation_id=CONVERSATION,
        session_id=SESSION,
        item_type=item_type,
        status=ItemStatus.OPEN if closed_seq is None else ItemStatus.COMPLETE,
        opened_seq=2,
        closed_seq=closed_seq,
        item_text=text,
        origin=origin,
        call_id=call_id,
        tool_name=tool_name,
        outcome=outcome,
        structured=structured,
        disclosure=disclosure,
        created_at=NOW,
        updated_at=NOW,
    )


def _event(
    seq: int,
    *,
    provenance: EventProvenance = EventProvenance.FRAME_RANGE,
    first: int | None = 3,
    last: int | None = 4,
    session_id: UUID | None = SESSION,
) -> ConversationEventRow:
    return ConversationEventRow(
        conversation_id=CONVERSATION,
        event_seq=seq,
        session_id=session_id,
        item_id=uuid4(),
        kind=ConversationEventKind.ITEM_COMPLETED,
        provenance=provenance,
        source_first_frame_seq=first,
        source_last_frame_seq=last,
        body={},
        created_at=NOW,
    )


def test_a_frame_derived_entry_names_the_session_whose_frames_it_was_read_off() -> None:
    """A conversation's entries span replaced sessions, so a frame range without its session
    could not be appealed — `read_frames` is session-keyed."""
    entry = item_entries.completed_entry(_item(ItemType.MESSAGE, text="hi"), _event(5))

    assert entry.provenance == FromFrames(session_id=SESSION, first_frame_seq=3, last_frame_seq=4)


def test_withheld_reasoning_has_no_summary_rather_than_an_empty_one() -> None:
    """`None` is "the backend disclosed nothing", which an empty string would misreport as "it
    disclosed an empty summary"."""
    entry = item_entries.completed_entry(
        _item(ItemType.REASONING, text="never disclosed", disclosure=ReasoningDisclosure.WITHHELD),
        _event(5, provenance=EventProvenance.AUTHORED, first=None, last=None),
    )

    assert isinstance(entry, ReasoningEntry)
    assert entry.summary is None


def test_a_prompt_speaks_in_the_voice_its_origin_recorded() -> None:
    entry = item_entries.completed_entry(
        _item(ItemType.PROMPT, text="do it", origin={"kind": "matrix", "address": "!r:x", "refs": ["$e"]}),
        _event(5, provenance=EventProvenance.AUTHORED, first=None, last=None),
    )

    assert isinstance(entry, PromptEntry)
    assert entry.origin == "matrix"


def test_a_completed_call_reports_its_outcome_and_structured_payload() -> None:
    entry = item_entries.completed_entry(
        _item(
            ItemType.TOOL_CALL,
            text="ok",
            call_id="toolu_1",
            tool_name="Bash",
            outcome=ToolOutcome.SUCCEEDED,
            structured={"exit_code": 0},
        ),
        _event(5),
    )

    assert isinstance(entry, ToolResultEntry)
    assert (entry.call_id, entry.outcome, entry.structured) == ("toolu_1", "succeeded", {"exit_code": 0})


def test_only_a_tool_call_has_an_entry_at_its_opening() -> None:
    """Every other item's entry is written where it completes — an item that never completed is
    not an entry, and prose is not whole until then."""
    with pytest.raises(ValueError, match="opening"):
        item_entries.opened_entry(_item(ItemType.MESSAGE, text="half"), _event(2))


def test_a_rows_provenance_and_its_frame_range_must_agree() -> None:
    with pytest.raises(ValueError, match="disagree"):
        item_entries.provenance_of(_event(5, provenance=EventProvenance.AUTHORED, first=3, last=4))


def test_an_ended_turn_is_an_authored_entry_with_the_runtimes_own_words() -> None:
    turn = ConversationTurn(
        turn_id=uuid4(),
        conversation_id=CONVERSATION,
        session_id=SESSION,
        first_seq=1,
        last_seq=9,
        started_at=NOW,
        ended_at=NOW,
        outcome=TurnOutcome.FAILED,
        failure="upstream is at capacity",
    )

    entry = item_entries.turn_end_entry(turn)

    assert entry.seq == 9
    assert entry.provenance == ConsoleAuthored()
    assert entry.end == TurnFailedEnd(failure="upstream is at capacity")


def test_a_running_turn_has_no_end_entry() -> None:
    turn = ConversationTurn(
        turn_id=uuid4(),
        conversation_id=CONVERSATION,
        session_id=SESSION,
        first_seq=1,
        last_seq=None,
        started_at=NOW,
        ended_at=None,
        outcome=None,
        failure=None,
    )

    with pytest.raises(ValueError, match="running"):
        item_entries.turn_end_entry(turn)


if __name__ == "__main__":
    pytest_bazel.main()
