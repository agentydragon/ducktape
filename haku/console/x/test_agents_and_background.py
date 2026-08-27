"""How a session that runs agents and background commands projects — and where it does not.

The shapes a console session reaches for once it is doing real work: a `Task` subagent, a `Bash`
backgrounded, the `BashOutput` loop that watches it, and a background task running while the
foreground keeps answering. None of them is in the recorded corpus that
<claude_code/test_projection.py> and <claude_code/test_diverse_session.py> read.

**These frames are composed, not captured**, so what each test may claim is bounded: it says what
the fold does with a shape, never that the CLI emits exactly that shape. Where a shape is a
hypothesis — the forwarded subagent frame above all — the test says so, and
<claude_code/testdata/> is where the capture that settles it will land
(<claude_code/frame_export_main.py>).

**The fold under test is the write path's.** `RuntimeTurnHandler.apply` is what `_run_turn`
drives frame by frame, and what it emits is what the log gets a row for; <claude_code/testing/fold.py>
is that same reducer over a whole capture, for a test holding a session rather than a turn.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest_bazel
from more_itertools import one

from haku.console.chat_models import ConversationEventKind, ToolOutcome
from haku.console.x import session_events
from haku.console.x.claude_code.projection import RecordedFrame
from haku.console.x.claude_code.runtime import ClaudeRuntimeAdapter
from haku.console.x.claude_code.testing.fold import whole_capture
from haku.console.x.claude_code.testing.wire import (
    assistant,
    recorded,
    result,
    system,
    text_block,
    tool_progress,
    tool_result,
    tool_use_block,
)
from haku.console.x.conversation_events import (
    ConversationEvent,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    ToolCallCompleted,
    ToolCallStarted,
)
from haku.runtime.x.bridge.protocol import HarnessFrame

# What the CLI answers a backgrounded `Bash` with: the call returns at once, naming a shell.
BACKGROUND_SHELL = {"shellId": "bash_1", "command": "sleep 60 && echo done", "stdout": "", "stderr": ""}


def _write_path(frames: Sequence[RecordedFrame]) -> tuple[ConversationEvent, ...]:
    """Every event the turn loop acts on, folded exactly as `_run_turn` folds — one frame at a time,
    with one state threaded across them."""
    handler = ClaudeRuntimeAdapter().turn_handler()
    said: list[ConversationEvent] = []
    for frame in frames:
        effects = handler.apply(frame_seq=frame.frame_seq, frame=HarnessFrame(frame=frame.payload))
        said.extend(effects.events)
    return tuple(said)


def _rows(events: Sequence[ConversationEvent]) -> list[ConversationEventKind]:
    """The `conversation_event` kinds those events are stored as, in order.

    One member of the vocabulary has no row at all, so this is shorter than the event list — which
    is the point of asking it rather than counting events.
    """
    return [kind for event in events if (stored := session_events.stored(event)) is not None for kind, _ in (stored,)]


def _subagent_frames(*, nested: bool) -> list[RecordedFrame]:
    """A `Task` call whose subagent's own turns come back on the same stream.

    **Hypothesis, not capture.** `protocol.md` says a forwarded subagent frame carries the parent
    call's id in `parent_tool_use_id`; the protocol describes that field for forwarded frames,
    because no `local_agent` task ever ran. *nested* folds the same session with and without it.
    """
    parent = "toolu_task" if nested else None
    return [
        recorded(
            1,
            assistant(
                tool_use_block("toolu_task", "Task", {"subagent_type": "general-purpose", "prompt": "find the tests"}),
                message_id="msg_A",
            ),
        ),
        recorded(2, assistant(text_block("looking"), message_id="msg_SUB", parent_tool_use_id=parent)),
        recorded(
            3,
            assistant(
                tool_use_block("toolu_inner", "Grep", {"pattern": "def test_"}),
                message_id="msg_SUB",
                parent_tool_use_id=parent,
            ),
        ),
        recorded(4, tool_result("toolu_inner", "3 matches", parent_tool_use_id=parent)),
        recorded(5, tool_result("toolu_task", "the subagent's report")),
        recorded(6, assistant(text_block("three test files"), message_id="msg_B")),
        # The turn's own end. Without it the last message stays open, because no reader may declare
        # a stream over: only a frame closes an item.
        recorded(7, result()),
    ]


def test_a_subagents_frames_project_exactly_as_the_sessions_own():
    """The fold reads no nesting at all: `parent_tool_use_id` is not in `projection.py`.

    So a subagent's prose becomes an ordinary message and its `Grep` an ordinary `ToolCallStarted`,
    attributed to the session with nothing saying whose work it was, and a transcript renders the
    subagent's inner turns inline.
    """
    assert whole_capture(_subagent_frames(nested=True)) == whole_capture(_subagent_frames(nested=False))


def test_the_call_that_spawned_the_subagent_belongs_to_no_message_at_all():
    """`msg_A` carried a `Task` call and no prose, so it opens no message item.

    That is the shape the vocabulary chose: a message is prose, and a call is a sibling item with
    its own lifecycle. What used to be an empty message row wrapping the call is now the two items
    that actually happened, and the answer arriving five frames later is paired by `call_id` rather
    than by which message's span contains it.
    """
    events = whole_capture(_subagent_frames(nested=True)).events
    said = {event.backend_item_id: event.provenance for event in events if isinstance(event, MessageCompleted)}

    assert said == {"msg_SUB": FrameRange(2, 2), "msg_B": FrameRange(6, 6)}
    answered = one(
        event for event in events if isinstance(event, ToolCallCompleted) and event.item.call_id == "toolu_task"
    )
    assert answered.provenance == FrameRange(5, 5)


def test_the_only_nested_frame_class_production_sends_is_unprojected():
    """`tool_progress` is absent from `protocol.md`, but the fold must still count it as
    unprojected. Routing on `parent_tool_use_id` would route heartbeats into a subagent
    view; this one has no case for the class, so it lands in the default branch and is counted."""
    projection = whole_capture(
        [
            recorded(1, assistant(tool_use_block("toolu_task", "Task", {"prompt": "find the tests"}))),
            recorded(2, tool_progress("toolu_inner", "Grep", parent_tool_use_id="toolu_task", elapsed_time_seconds=30)),
        ]
    )

    assert dict(projection.unprojected) == {"tool_progress": 1}


def _background_bash_frames() -> list[RecordedFrame]:
    """`Bash` with `run_in_background`, answered at once, and the task that outlives the answer."""
    return [
        recorded(
            1,
            assistant(
                tool_use_block("toolu_bg", "Bash", {"command": "sleep 60 && echo done", "run_in_background": True}),
                message_id="msg_A",
            ),
        ),
        recorded(2, tool_result("toolu_bg", "Running in background with ID bash_1", structured=BACKGROUND_SHELL)),
        recorded(
            3,
            system(
                "task_started",
                task_id="task_1",
                tool_use_id="toolu_bg",
                task_type="local_bash",
                description="sleep 60 && echo done",
            ),
        ),
        recorded(4, assistant(text_block("started it"), message_id="msg_B")),
        recorded(
            5,
            system(
                "task_notification", task_id="task_1", status="completed", summary="done", output_file="/tmp/task_1.log"
            ),
        ),
    ]


def test_a_backgrounded_call_completes_while_its_command_is_still_running():
    """`ToolCallCompleted` means the call returned, not that the command finished.

    The call is answered at frame 2 with `UNKNOWN` — the wire omits `is_error`, as it does on 56% of
    results — while the command it started runs on until frame 5. So a reader that treats a
    completed call as a finished command reports this one done a minute early, and the shell id that
    would let it know better is only inside `structured`.
    """
    events = whole_capture(_background_bash_frames()).events

    started = one(event for event in events if isinstance(event, ToolCallStarted))
    assert started.arguments["run_in_background"] is True
    completed = one(event for event in events if isinstance(event, ToolCallCompleted))
    assert (completed.item.call_id, completed.outcome, completed.provenance) == (
        "toolu_bg",
        ToolOutcome.UNKNOWN,
        FrameRange(2, 2),
    )
    assert completed.structured == BACKGROUND_SHELL


def test_the_background_tasks_own_frames_say_nothing_to_the_fold():
    """The command's start and end reach the default branch, so nothing in the conversation says
    the command finished — only that the call which asked for it returned.

    That is the deliberate loss: a task's identifiers and the harness's prose about it are Claude's
    concepts, and the neutral vocabulary carries none. The frames stay in `session_frames`.
    """
    projection = whole_capture(_background_bash_frames())

    assert projection.unprojected == {"system/task_started": 1, "system/task_notification": 1}
    assert not [event for event in projection.events if event.provenance in (FrameRange(3, 3), FrameRange(5, 5))]


def _monitor_frames(polls: int) -> list[RecordedFrame]:
    """A monitor loop: the same background shell read again and again, one call per read."""
    return [
        frame
        for poll in range(polls)
        for frame in (
            recorded(
                2 * poll + 1,
                assistant(
                    tool_use_block(f"toolu_poll_{poll}", "BashOutput", {"bash_id": "bash_1"}),
                    message_id=f"msg_poll_{poll}",
                ),
            ),
            recorded(
                2 * poll + 2,
                tool_result(f"toolu_poll_{poll}", "", structured={"shellId": "bash_1", "status": "running"}),
            ),
        )
    ]


def test_a_monitor_loop_leaves_no_prose_behind_at_all():
    """Five reads of one shell, and the transcript gains five calls and nothing else.

    What this used to gain was five empty assistant messages — one per poll, each a row a reader
    pages through and a reply the room had to be stopped from being told. Prose is the only thing a
    message holds now and these polls produced none, so the empty rows do not exist to be filtered.

    Nothing says the five are one shell being watched: the only thing they share is an argument
    value, which nothing indexes.
    """
    frames = _monitor_frames(polls=5)
    events = _write_path(frames)

    started = [event for event in events if isinstance(event, ToolCallStarted)]
    assert len(started) == 5
    assert not [event for event in events if isinstance(event, MessageCompleted | ItemSegment)]
    assert {tuple(event.arguments.items()) for event in started} == {(("bash_id", "bash_1"),)}
    assert len({event.call_id for event in started}) == 5
    # Nothing was deduped: two identical reads are two calls, told apart only by their ids.
    assert _rows(events).count(ConversationEventKind.ITEM_COMPLETED) == 5


def _interleaved_frames() -> list[RecordedFrame]:
    """One message's prose either side of a tool result — the frames of one `message.id`, split."""
    return [
        recorded(1, assistant(text_block("starting "), message_id="msg_A")),
        recorded(2, tool_result("toolu_bg", "Running in background with ID bash_1", structured=BACKGROUND_SHELL)),
        recorded(3, assistant(text_block("and meanwhile"), message_id="msg_A")),
    ]


def test_the_write_path_no_longer_splits_a_message_across_its_frames():
    """What threading one state across a turn's frames bought.

    Seeded per frame, `msg_A` completed twice — one item per frame of prose, so the room read one
    answer as two. Now the frames of one message are one item: the write path emits its segments as
    they arrive and leaves it open, which is what lets the store append to the row its predecessor
    opened.
    """
    events = _write_path(_interleaved_frames())

    assert _rows(events) == [
        ConversationEventKind.ITEM_STARTED,
        ConversationEventKind.ITEM_SEGMENT,
        ConversationEventKind.ITEM_SEGMENT,
        ConversationEventKind.ITEM_COMPLETED,
        ConversationEventKind.ITEM_SEGMENT,
    ]


def test_folding_a_capture_and_writing_a_turn_are_one_fold():
    """They used to differ by one event: a whole-log read declared the stream over and closed the
    message the write path left open. Nothing declares that now — a transcript is folded from the
    stored log, so an item the frames left open stays open in both, and what closes it is a frame
    that says so or `session_store.close_answer` in the transaction that ends the turn.
    """
    frames = _interleaved_frames()

    read, write = whole_capture(frames).events, _write_path(frames)

    assert read == write
    assert not [event for event in read if isinstance(event, MessageCompleted)]


def test_a_foreground_message_spans_the_background_frames_the_fold_ignores():
    """The foreground message spans frames 1 to 4 because the wire kept sending prose under one
    `message.id` — the background task's own frames at 2 and 5 project to nothing and end nothing.
    So a reader finding an event by range containment sees a span with holes in it, and what
    happened in those holes is only in `session_frames`.
    """
    frames = [
        recorded(1, assistant(text_block("running it "), message_id="msg_A")),
        recorded(
            2,
            system(
                "task_started",
                task_id="task_1",
                tool_use_id="toolu_bg",
                task_type="local_bash",
                description="make test",
            ),
        ),
        recorded(3, tool_result("toolu_bg", "Running in background with ID bash_1", structured=BACKGROUND_SHELL)),
        recorded(4, assistant(text_block("meanwhile"), message_id="msg_A")),
        recorded(5, system("task_notification", task_id="task_1", status="completed", summary="ok")),
        recorded(6, assistant(text_block("and it passed"), message_id="msg_B")),
    ]
    events = whole_capture(frames).events

    foreground = one(
        event for event in events if isinstance(event, MessageCompleted) and event.backend_item_id == "msg_A"
    )
    assert foreground.provenance == FrameRange(1, 4)
    assert not [event for event in events if event.provenance in (FrameRange(2, 2), FrameRange(5, 5))]


if __name__ == "__main__":
    pytest_bazel.main()
