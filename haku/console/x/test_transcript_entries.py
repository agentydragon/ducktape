"""The neutral vocabulary as the MCP surface hands it out.

Driven through the real `project_log()` rather than hand-built events: a mapping tested against its
own hand-written input can agree with itself while disagreeing with the projection.
"""

import pytest_bazel
from more_itertools import one

from haku.console.x import conversation_records, transcript_entries
from haku.console.x.claude_code.projection import ProjectionState, RecordedFrame, project, project_log
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


def test_segments_are_folded_into_the_item_they_belong_to() -> None:
    """The vocabulary emits prose as increments so a live channel can print them as they arrive; a
    transcript is read after the fact and wants the item, whose text is exactly those increments."""
    projection = project_log(
        [
            recorded(1, assistant(text_block("half "), message_id="msg_1")),
            recorded(2, assistant(text_block("an answer"), message_id="msg_1")),
        ]
    )

    entry = one(transcript_entries.entries(projection))

    assert isinstance(entry, conversation_records.MessageEntry)
    assert entry.text == "half an answer"


def test_an_entry_is_numbered_by_its_position_among_the_entries() -> None:
    """The index is the cursor's key, so it has to count what a reader will actually receive —
    which is one entry per finished item, not one per event the fold emitted. In the order the items
    finished, which is why `msg_1` lands before the thinking that came after it."""
    projection = project_log(
        [
            recorded(1, assistant(text_block("hello"), message_id="msg_1")),
            recorded(2, assistant(thinking_block("hmm"), message_id="msg_2")),
            recorded(3, assistant(text_block("and here"), message_id="msg_2")),
            _result(4),
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


def test_a_tool_call_and_its_answer_are_joined_by_call_id_and_nothing_else() -> None:
    """They are two entries because the call is real while it is still running — the answer can be
    arbitrarily many frames later, or never arrive at all."""
    projection = project_log(
        [
            recorded(1, assistant(tool_use_block("toolu_1", "Read", {"path": "/x"}), message_id="msg_1")),
            recorded(2, tool_result("toolu_1", "file contents", structured={"filePath": "/x"})),
        ]
    )

    call, answer = transcript_entries.entries(projection)

    assert isinstance(call, conversation_records.ToolCallEntry)
    assert isinstance(answer, conversation_records.ToolResultEntry)
    assert (call.tool_name, call.call_id) == ("Read", "toolu_1")
    assert answer.call_id == "toolu_1"
    assert answer.content == "file contents"
    assert answer.structured == {"filePath": "/x"}


def test_an_item_the_turn_never_finished_is_not_an_entry() -> None:
    """A turn that died mid-message left prose nothing finished saying, and a transcript printing it
    would report a half-sentence as what was said."""
    state, projection = project(ProjectionState(), [recorded(1, assistant(text_block("half a "), message_id="msg_1"))])

    assert state.open_message is not None
    assert transcript_entries.entries(projection) == []


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
    assert entry.end == conversation_records.TurnAnsweredEnd()


def test_frame_classes_this_release_cannot_read_are_reported_rather_than_dropped() -> None:
    """A transcript quietly missing something is worse than one that says what it missed."""
    projection = project_log([RecordedFrame(frame_seq=1, payload={"type": "tool_progress"})])

    assert transcript_entries.entries(projection) == []
    assert transcript_entries.unreadable(projection) == {"tool_progress": 1}


def test_nothing_unreadable_is_absent_rather_than_an_empty_map() -> None:
    assert transcript_entries.unreadable(project_log([_result(1)])) is None


if __name__ == "__main__":
    pytest_bazel.main()
