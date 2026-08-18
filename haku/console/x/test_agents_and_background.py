"""How a session that runs agents and background commands projects — and where it does not.

The shapes a console session reaches for once it is doing real work: a `Task` subagent, a `Bash`
backgrounded, the `BashOutput` loop that watches it, and a background task running while the
foreground keeps answering. None of them is in the recorded corpus that
<claude_code/test_projection.py> and <claude_code/test_diverse_session.py> read — every
`task_started` the census saw was `local_bash` and no subagent ever ran
(<../debug/frame_shape_census.md> § Loose ends).

**These frames are composed, not captured**, so what each test may claim is bounded: it says what
the fold does with a shape, never that the CLI emits exactly that shape. Where a shape is a
hypothesis — the forwarded subagent frame above all — the test says so, and
<claude_code/testdata/> is where the capture that settles it will land
(<README.md> § Recording a session as a fixture).

**Both folds are asserted, because they answer different questions.** `project_log` is the read
path's and merges the frames sharing one `message.id`; `frame_projection.projected` is the write
path's own, seeded fresh per frame, and its `MessageCompleted` count is a count of
`session_messages` rows and of replies the room is told about.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest_bazel
from more_itertools import one

from haku.console.chat_models import AuthoredEventKind, ConversationEventKind
from haku.console.x import session_events
from haku.console.x.claude_code.projection import RecordedFrame, project_log
from haku.console.x.claude_code.testing.wire import (
    assistant,
    recorded,
    system,
    text_block,
    tool_progress,
    tool_result,
    tool_use_block,
)
from haku.console.x.conversation_events import (
    ConversationEvent,
    FrameRange,
    MessageCompleted,
    Outcome,
    ToolCallCompleted,
    ToolCallStarted,
)
from haku.console.x.frame_projection import projected
from util.sqlalchemy_types import UnknownValue

# What the CLI answers a backgrounded `Bash` with: the call returns at once, naming a shell.
BACKGROUND_SHELL = {"shellId": "bash_1", "command": "sleep 60 && echo done", "stdout": "", "stderr": ""}

# The halves of a stored row this file never looks at: the fold decides the kind, the provenance
# and the body, and these are the caller's.
_ROW_OWNER = UUID("00000000-0000-4000-8000-000000000009")
_ROW_CLOCK = datetime.fromtimestamp(0, UTC)


def _write_path(frames: Sequence[RecordedFrame]) -> tuple[ConversationEvent, ...]:
    """Every event the turn loop acts on, folded exactly as `_run_turn` folds — one frame at a
    time, seeded empty."""
    return tuple(event for frame in frames for event in projected(frame_seq=frame.frame_seq, payload=frame.payload))


def _rows(events: Sequence[ConversationEvent]) -> list[ConversationEventKind | AuthoredEventKind | UnknownValue]:
    """The `session_events` rows those events are stored as, in order. Two members of the
    vocabulary have no row at all, so this is shorter than the event list — which is the point of
    asking it rather than counting events.

    `UnknownValue` is in the type because the column admits one, never because a row minted here
    could carry it: nothing this process wrote can be a kind this process cannot name."""
    return [
        row.kind
        for event in events
        if (row := session_events.row(event, session_id=_ROW_OWNER, turn_id=_ROW_OWNER, now=_ROW_CLOCK)) is not None
    ]


def _subagent_frames(*, nested: bool) -> list[RecordedFrame]:
    """A `Task` call whose subagent's own turns come back on the same stream.

    **Hypothesis, not capture.** `protocol.md` says a forwarded subagent frame carries the parent
    call's id in `parent_tool_use_id`; the census saw that field non-null on `tool_progress` alone,
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
    ]


def test_a_subagents_frames_project_exactly_as_the_sessions_own():
    """The fold reads no nesting at all: `parent_tool_use_id` is not in `projection.py`.

    So a subagent's message becomes an ordinary `MessageCompleted` and its `Grep` an ordinary
    `ToolCallStarted`, attributed to the session with nothing on the row saying whose work it was,
    and a transcript renders the subagent's inner turns inline.
    """
    assert project_log(_subagent_frames(nested=True)) == project_log(_subagent_frames(nested=False))


def test_the_task_call_and_its_answer_end_up_in_different_messages():
    """The parent's message closes at the subagent's first frame, five frames before its own answer.

    A different `message.id` is what ends a message, and the subagent's frames carry one — so
    `msg_A`'s span stops at frame 1 while the `ToolCallCompleted` that answers its `Task` call
    lands at frame 5, inside no message's span at all. The subagent's own message, meanwhile,
    spans frames 2 and 3 and contains neither of them.
    """
    events = project_log(_subagent_frames(nested=True)).events
    messages = {event.agent_message_id: event.provenance for event in events if isinstance(event, MessageCompleted)}

    assert messages == {"msg_A": FrameRange(1, 1), "msg_SUB": FrameRange(2, 3), "msg_B": FrameRange(6, 6)}
    answered = one(event for event in events if isinstance(event, ToolCallCompleted) and event.call_id == "toolu_task")
    assert answered.provenance == FrameRange(5, 5)


def test_the_only_nested_frame_class_production_sends_is_unprojected():
    """`tool_progress` — 113 frames, absent from `protocol.md`, and the census's one non-null
    `parent_tool_use_id`. A fold that routed on that field would route heartbeats into a subagent
    view; this one has no case for the class, so it lands in the default branch and is counted."""
    projection = project_log(
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

    The call is answered at frame 2 with `Outcome.UNKNOWN` — the wire omits `is_error`, as it does
    on 56% of results — while the command it started runs on until frame 5. So a reader that treats
    a completed call as a finished command reports this one done a minute early, and the shell id
    that would let it know better is only inside `structured`.
    """
    events = project_log(_background_bash_frames()).events

    started = one(event for event in events if isinstance(event, ToolCallStarted))
    assert started.arguments["run_in_background"] is True
    completed = one(event for event in events if isinstance(event, ToolCallCompleted))
    assert (completed.call_id, completed.outcome, completed.provenance) == (
        "toolu_bg",
        Outcome.UNKNOWN,
        FrameRange(2, 2),
    )
    assert completed.structured == BACKGROUND_SHELL


def test_the_background_tasks_own_frames_say_nothing_to_the_fold():
    """The command's start and end reach the default branch, so nothing in the conversation says
    the command finished — only that the call which asked for it returned.

    That is the deliberate loss: a task's identifiers and the harness's prose about it are Claude's
    concepts, and the neutral vocabulary carries none. The frames stay in `session_frames`.
    """
    projection = project_log(_background_bash_frames())

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


def test_a_monitor_loop_mints_one_empty_assistant_message_per_poll():
    """Five reads of one shell, and the transcript gains five messages that say nothing.

    Under the write path's fold each `MessageCompleted` closes a `session_messages` row, so this is
    five rows with empty content — not queued to the room, since `_enqueue_reply` drops an empty
    body, but five a transcript reader pages through. Nothing says they are one shell being watched:
    the only thing they share is an argument value, which no row indexes.
    """
    frames = _monitor_frames(polls=5)
    events = _write_path(frames)

    started = [event for event in events if isinstance(event, ToolCallStarted)]
    messages = [event for event in events if isinstance(event, MessageCompleted)]
    assert len(started) == len(messages) == 5
    assert {tuple(event.arguments.items()) for event in started} == {(("bash_id", "bash_1"),)}
    assert len({event.call_id for event in started}) == 5
    assert all(message.text is None for message in messages)
    # Nothing was deduped: two identical reads are two rows, told apart only by their call ids.
    assert _rows(events).count(ConversationEventKind.TOOL_CALL_COMPLETED) == 5


def test_the_two_folds_produce_a_monitor_loops_events_in_different_orders():
    """Same events, different sequence — the read path defers a message past the tool result and
    the write path does not.

    Each poll is its own `message.id`, so nothing here is merged and the two folds produce the same
    nine events. They still do not agree positionally: `project_log` closes a message only when the
    next one opens, so its `MessageCompleted` lands *after* the poll's result, while the per-frame
    fold emits it *before*. A check comparing the two by position would report drift on every
    monitor loop, which is why `reprojection` aligns by frame.
    """
    frames = _monitor_frames(polls=3)
    read, write = project_log(frames).events, _write_path(frames)

    assert Counter(type(event) for event in read) == Counter(type(event) for event in write)
    assert [type(event).__name__ for event in read][:3] == ["ToolCallStarted", "ToolCallCompleted", "MessageCompleted"]
    assert [type(event).__name__ for event in write][:3] == ["ToolCallStarted", "MessageCompleted", "ToolCallCompleted"]


def test_a_foreground_message_spans_the_background_frames_the_fold_ignores():
    """The foreground message spans frames 1 to 4 because the wire kept sending frames under one
    `message.id` — the background task's own frames at 2 and 5 project to nothing and end nothing.
    So a reader finding an event's message by range containment sees a span with holes in it, and
    what happened in those holes is only in `session_frames`.
    """
    frames = [
        recorded(
            1,
            assistant(
                tool_use_block("toolu_bg", "Bash", {"command": "make test", "run_in_background": True}),
                message_id="msg_A",
            ),
        ),
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
    events = project_log(frames).events

    foreground = one(
        event for event in events if isinstance(event, MessageCompleted) and event.agent_message_id == "msg_A"
    )
    assert foreground.provenance == FrameRange(1, 4)
    assert not [event for event in events if event.provenance in (FrameRange(2, 2), FrameRange(5, 5))]


def test_the_write_path_splits_that_message_in_two_and_keeps_one_id_on_both():
    """The same interleaved turn through the fold that actually writes rows.

    Seeded per frame, `msg_A` completes twice — once at frame 1 and once at frame 3 — so
    `session_messages` holds two rows carrying one `agent_message_id`. A reader keying on that id
    gets two answers, which is why nothing does.
    """
    frames = [
        recorded(
            1,
            assistant(
                tool_use_block("toolu_bg", "Bash", {"command": "make test", "run_in_background": True}),
                message_id="msg_A",
            ),
        ),
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
        recorded(3, assistant(text_block("meanwhile"), message_id="msg_A")),
    ]
    events = _write_path(frames)

    assert [event.provenance for event in events if isinstance(event, MessageCompleted)] == [
        FrameRange(1, 1),
        FrameRange(3, 3),
    ]
    assert {event.agent_message_id for event in events if isinstance(event, MessageCompleted)} == {"msg_A"}
    assert _rows(events) == [
        ConversationEventKind.TOOL_CALL_STARTED,
        ConversationEventKind.MESSAGE_COMPLETED,
        ConversationEventKind.MESSAGE_COMPLETED,
    ]


if __name__ == "__main__":
    pytest_bazel.main()
