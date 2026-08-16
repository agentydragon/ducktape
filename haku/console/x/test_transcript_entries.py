"""The neutral vocabulary as the MCP surface hands it out.

Driven through the real `project()` rather than hand-built events: the property worth pinning is
that what a caller reads is what the fold produced, and a mapping tested against its own
hand-written input can agree with itself while disagreeing with the projection.
"""

from typing import Any

import pytest_bazel
from more_itertools import one

from haku.console.tools import conversations
from haku.console.x import transcript_entries
from haku.console.x.claude_projection import RecordedFrame, project


def _assistant(frame_seq: int, message_id: str, block: dict[str, Any]) -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={"message": {"content": [block], "id": message_id, "role": "assistant"}, "type": "assistant"},
    )


def _tool_result(frame_seq: int, call_id: str, content: Any, structured: Any) -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "message": {
                "content": [{"content": content, "tool_use_id": call_id, "type": "tool_result"}],
                "role": "user",
            },
            "tool_use_result": structured,
            "type": "user",
        },
    )


def _result(frame_seq: int) -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "duration_ms": 41_902,
            "subtype": "success",
            "total_cost_usd": 0.4213,
            "type": "result",
            "usage": {"cache_read_input_tokens": 133_907, "input_tokens": 4, "output_tokens": 91},
        },
    )


def test_deltas_do_not_reach_the_read_surface() -> None:
    """A message's deltas concatenate to exactly the text its `message` entry carries, so
    streaming them to a reader of a finished conversation is the same prose twice."""
    projection = project(
        [
            _assistant(1, "msg_1", {"text": "half ", "type": "text"}),
            _assistant(2, "msg_1", {"text": "an answer", "type": "text"}),
        ]
    )

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversations.MessageEntry)
    assert entry.text == "half an answer"


def test_an_entry_is_numbered_by_its_position_after_the_deltas_are_gone() -> None:
    """The index is the cursor's key, so it has to count what a reader will actually receive."""
    projection = project(
        [
            _assistant(1, "msg_1", {"text": "hello", "type": "text"}),
            _assistant(2, "msg_2", {"signature": "Eq", "thinking": "hmm", "type": "thinking"}),
            _result(3),
        ]
    )

    assert [(entry.index, entry.kind) for entry in transcript_entries.entries(projection)] == [
        (0, "message"),
        (1, "reasoning"),
        (2, "message"),
        (3, "turn_end"),
    ]


def test_a_multi_frame_message_reports_the_span_it_was_read_off() -> None:
    """The appeal path: an operator disputing a normalization reads the frames behind it, and a
    message that spans several has to name all of them."""
    projection = project(
        [
            _assistant(4, "msg_1", {"text": "one ", "type": "text"}),
            _assistant(6, "msg_1", {"text": "two", "type": "text"}),
        ]
    )

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversations.MessageEntry)
    assert entry.provenance == conversations.FromFrames(first_frame_seq=4, last_frame_seq=6)
    assert entry.message.opened_at_frame_seq == 4


def test_a_tool_call_and_its_answer_are_joined_by_call_id_and_nothing_else() -> None:
    """They are two entries because the call is real while it is still running — the answer can be
    arbitrarily many frames later, or never arrive at all."""
    projection = project(
        [
            _assistant(1, "msg_1", {"id": "toolu_1", "input": {"path": "/x"}, "name": "Read", "type": "tool_use"}),
            _tool_result(2, "toolu_1", "file contents", {"filePath": "/x"}),
        ]
    )

    # The third entry is the message closing at the end of the input, which every fold emits.
    call, answer, _ = transcript_entries.entries(projection)

    assert isinstance(call, conversations.ToolCallEntry)
    assert isinstance(answer, conversations.ToolResultEntry)
    assert (call.tool_name, call.call_id) == ("Read", "toolu_1")
    assert answer.call_id == "toolu_1"
    assert answer.content == conversations.ResultText(text="file contents")
    assert answer.structured == {"filePath": "/x"}


def test_an_absent_is_error_stays_unknown_rather_than_reading_as_fine() -> None:
    """The field is routinely absent, so a two-valued outcome would report every unanswerable
    case as a success."""
    projection = project([_tool_result(1, "toolu_1", "output", None)])

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversations.ToolResultEntry)
    assert entry.outcome == conversations.Outcome.UNKNOWN


def test_a_turn_end_carries_the_accounting_in_neutral_terms() -> None:
    projection = project([_result(9)])

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversations.TurnEndEntry)
    assert entry.outcome == "answered"
    assert entry.usage == conversations.TurnUsage(
        input_tokens=4, output_tokens=91, cached_input_tokens=133_907, cost_usd=0.4213, duration_ms=41_902
    )


def test_frame_classes_this_release_cannot_read_are_reported_rather_than_dropped() -> None:
    """A transcript quietly missing something is worse than one that says what it missed."""
    projection = project([RecordedFrame(frame_seq=1, payload={"type": "tool_progress"})])

    assert transcript_entries.entries(projection) == []
    assert transcript_entries.unreadable(projection) == {"tool_progress": 1}


def test_nothing_unreadable_is_absent_rather_than_an_empty_map() -> None:
    assert transcript_entries.unreadable(project([_result(1)])) is None


if __name__ == "__main__":
    pytest_bazel.main()
