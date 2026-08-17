"""The neutral vocabulary as the MCP surface hands it out.

Driven through the real `project_log()` rather than hand-built events: the property worth pinning is
that what a caller reads is what the fold produced, and a mapping tested against its own
hand-written input can agree with itself while disagreeing with the projection.
"""

import pytest_bazel
from more_itertools import one

from haku.console.x import conversation_records, transcript_entries
from haku.console.x.claude_code.projection import RecordedFrame, project_log
from haku.console.x.claude_code.testing.wire import (
    assistant,
    recorded,
    result,
    text_block,
    thinking_block,
    tool_result,
    tool_use_block,
)


def _result(frame_seq: int) -> RecordedFrame:
    return recorded(frame_seq, result())


def test_deltas_do_not_reach_the_read_surface() -> None:
    """A message's deltas concatenate to exactly the text its `message` entry carries, so
    streaming them to a reader of a finished conversation is the same prose twice."""
    projection = project_log(
        [
            recorded(1, assistant(text_block("half "), message_id="msg_1")),
            recorded(2, assistant(text_block("an answer"), message_id="msg_1")),
        ]
    )

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversation_records.MessageEntry)
    assert entry.text == "half an answer"


def test_an_entry_is_numbered_by_its_position_after_the_deltas_are_gone() -> None:
    """The index is the cursor's key, so it has to count what a reader will actually receive."""
    projection = project_log(
        [
            recorded(1, assistant(text_block("hello"), message_id="msg_1")),
            recorded(2, assistant(thinking_block("hmm"), message_id="msg_2")),
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
    projection = project_log(
        [
            recorded(4, assistant(text_block("one "), message_id="msg_1")),
            recorded(6, assistant(text_block("two"), message_id="msg_1")),
        ]
    )

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversation_records.MessageEntry)
    assert entry.provenance == conversation_records.FromFrames(first_frame_seq=4, last_frame_seq=6)
    assert entry.message.opened_at_frame_seq == 4


def test_a_tool_call_and_its_answer_are_joined_by_call_id_and_nothing_else() -> None:
    """They are two entries because the call is real while it is still running — the answer can be
    arbitrarily many frames later, or never arrive at all."""
    projection = project_log(
        [
            recorded(1, assistant(tool_use_block("toolu_1", "Read", {"path": "/x"}), message_id="msg_1")),
            recorded(2, tool_result("toolu_1", "file contents", structured={"filePath": "/x"})),
        ]
    )

    # The third entry is the message `project_log` closes when the log ends.
    call, answer, _ = transcript_entries.entries(projection)

    assert isinstance(call, conversation_records.ToolCallEntry)
    assert isinstance(answer, conversation_records.ToolResultEntry)
    assert (call.tool_name, call.call_id) == ("Read", "toolu_1")
    assert answer.call_id == "toolu_1"
    assert answer.content == conversation_records.ResultText(text="file contents")
    assert answer.structured == {"filePath": "/x"}


def test_an_absent_is_error_stays_unknown_rather_than_reading_as_fine() -> None:
    """The field is routinely absent, so a two-valued outcome would report every unanswerable
    case as a success."""
    projection = project_log([recorded(1, tool_result("toolu_1", "output"))])

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversation_records.ToolResultEntry)
    assert entry.outcome == conversation_records.Outcome.UNKNOWN


def test_a_result_frame_reaches_the_read_surface_as_the_turn_ending() -> None:
    projection = project_log([_result(9)])

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversation_records.TurnEndEntry)
    assert entry.outcome == "answered"


def test_frame_classes_this_release_cannot_read_are_reported_rather_than_dropped() -> None:
    """A transcript quietly missing something is worse than one that says what it missed."""
    projection = project_log([RecordedFrame(frame_seq=1, payload={"type": "tool_progress"})])

    assert transcript_entries.entries(projection) == []
    assert transcript_entries.unreadable(projection) == {"tool_progress": 1}


def test_nothing_unreadable_is_absent_rather_than_an_empty_map() -> None:
    assert transcript_entries.unreadable(project_log([_result(1)])) is None


if __name__ == "__main__":
    pytest_bazel.main()
