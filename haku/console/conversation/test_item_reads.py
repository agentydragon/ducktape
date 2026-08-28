"""How one `conversation_item` row becomes the item every reader is served."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from haku.console.chat_models import ItemStatus, ItemType, ToolOutcome
from haku.console.conversation import item_reads
from haku.console.conversation.conversation_event import ReasoningDisclosure, TurnFailed, TurnOutcome
from haku.console.conversation.item_reads import (
    ConsoleAuthored,
    ConversationPageRow,
    FromFrames,
    MessageItem,
    PromptItem,
    ReasoningItem,
    ToolCallItem,
)
from haku.console.database_schema import ConversationItem, ConversationTurn

CONVERSATION = UUID("44444444-4444-4444-4444-444444444444")
SESSION = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)


def _row(
    item_type: ItemType,
    *,
    status: ItemStatus = ItemStatus.COMPLETE,
    text: str = "",
    closed_seq: int | None = 5,
    origin: dict[str, Any] | None = None,
    call_id: str | None = None,
    tool_name: str | None = None,
    outcome: ToolOutcome | None = None,
    structured: Any | None = None,
    disclosure: ReasoningDisclosure | None = None,
    span: tuple[int, int] | None = (3, 4),
    session_id: UUID | None = SESSION,
) -> ConversationPageRow:
    item = ConversationItem(
        item_id=uuid4(),
        conversation_id=CONVERSATION,
        session_id=session_id,
        item_type=item_type,
        status=status,
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
    first, last = span if span is not None else (None, None)
    return ConversationPageRow(item=item, first_frame_seq=first, last_frame_seq=last)


def test_an_item_is_the_row_at_its_opening_position_with_its_lifecycle() -> None:
    item = item_reads.item_of(_row(ItemType.MESSAGE, text="hi"))

    assert isinstance(item, MessageItem)
    assert (item.opened_seq, item.closed_seq, item.status, item.text) == (2, 5, ItemStatus.COMPLETE, "hi")


def test_a_frame_derived_item_names_the_session_whose_frames_it_was_read_off() -> None:
    """A conversation's items span replaced sessions, so a frame range without its session
    could not be appealed — `read_session_frames` is session-keyed."""
    item = item_reads.item_of(_row(ItemType.MESSAGE, text="hi"))

    assert item.provenance == FromFrames(session_id=SESSION, first_frame_seq=3, last_frame_seq=4)


def test_a_row_with_no_frame_derived_events_is_console_authored() -> None:
    item = item_reads.item_of(_row(ItemType.PROMPT, text="do it", origin={"kind": "spa"}, span=None))

    assert item.provenance == ConsoleAuthored()


def test_a_prompt_speaks_in_the_voice_its_origin_recorded() -> None:
    item = item_reads.item_of(
        _row(ItemType.PROMPT, text="do it", origin={"kind": "matrix", "address": "!r:x", "refs": ["$e"]}, span=None)
    )

    assert isinstance(item, PromptItem)
    assert item.origin == "matrix"


def test_reasoning_carries_its_text_and_disclosure_as_stored() -> None:
    """`withheld` is what says the text is not the thought — the row is presented, not edited."""
    item = item_reads.item_of(_row(ItemType.REASONING, text="never disclosed", disclosure=ReasoningDisclosure.WITHHELD))

    assert isinstance(item, ReasoningItem)
    assert (item.text, item.disclosure) == ("never disclosed", ReasoningDisclosure.WITHHELD)


def test_a_tool_call_is_one_item_with_the_answer_where_one_arrived() -> None:
    item = item_reads.item_of(
        _row(
            ItemType.TOOL_CALL,
            text="ok",
            call_id="toolu_1",
            tool_name="Bash",
            outcome=ToolOutcome.SUCCEEDED,
            structured={"exit_code": 0},
        )
    )

    assert isinstance(item, ToolCallItem)
    assert (item.call_id, item.tool_name, item.content, item.outcome, item.structured) == (
        "toolu_1",
        "Bash",
        "ok",
        "succeeded",
        {"exit_code": 0},
    )


def test_a_call_not_yet_answered_has_a_null_outcome_rather_than_a_guessed_one() -> None:
    item = item_reads.item_of(
        _row(ItemType.TOOL_CALL, status=ItemStatus.OPEN, closed_seq=None, call_id="toolu_1", tool_name="Bash")
    )

    assert isinstance(item, ToolCallItem)
    assert (item.status, item.outcome, item.content) == (ItemStatus.OPEN, None, "")


def test_an_open_row_is_served_open_with_its_text_so_far() -> None:
    item = item_reads.item_of(_row(ItemType.MESSAGE, status=ItemStatus.OPEN, closed_seq=None, text="half an ans"))

    assert isinstance(item, MessageItem)
    assert (item.status, item.closed_seq, item.text) == (ItemStatus.OPEN, None, "half an ans")


def test_a_cut_off_row_is_served_failed_with_what_had_been_said() -> None:
    """Failing a session keeps its open items' prose under `status=failed` — the read presents the
    row rather than dropping it or reporting it finished."""
    item = item_reads.item_of(_row(ItemType.MESSAGE, status=ItemStatus.FAILED, text="the answer was going to"))

    assert isinstance(item, MessageItem)
    assert (item.status, item.text) == (ItemStatus.FAILED, "the answer was going to")


def test_a_frame_span_with_no_session_is_a_defect_not_an_item() -> None:
    with pytest.raises(ValueError, match="session"):
        item_reads.item_of(_row(ItemType.MESSAGE, text="hi", session_id=None))


def test_an_ended_turn_reports_the_runtimes_own_words() -> None:
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

    assert item_reads.turn_end_of(turn) == TurnFailed(failure="upstream is at capacity")


def test_a_running_turn_has_no_end() -> None:
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

    assert item_reads.turn_end_of(turn) is None


if __name__ == "__main__":
    pytest_bazel.main()
