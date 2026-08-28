"""What the projection does with a subagent and a backgrounded command, taken off the wire.

<test_diverse_session.py>'s capture holds one straight-line session: text, thinking, tool calls,
one failure. This one holds the shapes that capture has none of — a command the CLI ran in the
background and reported on later, and a **subagent**, whose frames carry a `parent_tool_use_id`
naming the call that spawned it (`testdata/agents_and_background.jsonl`, redacted).

Those shapes had never been recorded anywhere. Against the whole production frame log — 35,791
frames on 2026-08-16 — `local_agent` occurs **zero** times and so does `parent_tool_use_id`, so
until this capture the only evidence about a subagent's wire shape was `protocol.md`. That is why
this file asserts current behaviour rather than desired behaviour: the assertions below that pin
something looking wrong say so, and changing the fold is a separate change with this fixture
already in place to measure it.
"""

import json
from collections import Counter
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from haku.console.chat_models import ToolOutcome
from haku.console.conversation.conversation_event import FrameRange
from haku.console.x.claude_code.projection import RecordedFrame
from haku.console.x.claude_code.testing.fold import whole_capture
from haku.console.x.conversation_events import (
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    Projection,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnAnswered,
    TurnCompleted,
)
from util.bazel.runfiles import get_required_path

_CAPTURE = get_required_path("ducktape/haku/console/x/claude_code/testdata/agents_and_background.jsonl")

_RECORDS: list[dict[str, Any]] = [json.loads(line) for line in _CAPTURE.read_text().splitlines()]

# As in `test_diverse_session.py`: a record's index is its `frame_seq`, so a failure quotes a line.
_FRAMES = [
    RecordedFrame(frame_seq=index, payload=record["frame"])
    for index, record in enumerate(_RECORDS)
    if "frame" in record
]

# The `Bash` call that asked for `run_in_background`, and the background task the CLI opened for it.
_BASH_CALL = "toolu_47"
_BASH_TASK = "id-58"
# The `Agent` call that spawned the subagent, and its task.
_AGENT_CALL = "toolu_152"
_AGENT_TASK = "id-161"


@pytest.fixture(scope="module")
def projection() -> Projection:
    return whole_capture(_FRAMES)


def test_the_capture_folds_to_two_turns(projection: Projection):
    """Two turns, because the CLI re-initialised mid-capture (a second `system/init` at record 185)
    rather than because the subagent's completion closed anything — both `result` frames have a null
    `parent_tool_use_id`."""
    assert Counter(type(event) for event in projection.events) == {
        ReasoningStarted: 7,
        ReasoningCompleted: 7,
        MessageStarted: 7,
        MessageCompleted: 7,
        # One per message, one per reasoning item, and one per tool result that had prose to show.
        ItemSegment: 18,
        ToolCallStarted: 4,
        ToolCallCompleted: 4,
        TurnCompleted: 2,
    }
    assert [event.end for event in projection.events if isinstance(event, TurnCompleted)] == [
        TurnAnswered(),
        TurnAnswered(),
    ]


def test_the_background_task_frame_classes_are_all_unprojected(projection: Projection):
    """Every frame the CLI sends about a background task reaches the fold's default branch — the
    live task list, a patch carrying a terminal status, and the task's own start and end.

    The last two are the deliberate loss: what a `task_started` said about a step in flight was
    Claude's concept keyed by Claude's identifiers, so it has no neutral spelling. Named here so
    that a release carrying something the projection needs fails rather than silently incrementing
    a counter."""
    assert projection.unprojected == {
        "system/background_tasks_changed": 4,
        "system/task_updated": 2,
        "system/task_started": 2,
        "system/task_notification": 2,
    }


def test_a_backgrounded_call_succeeds_long_before_its_command_ends(projection: Projection):
    """`ToolCallCompleted` means the call returned a shell id, not that the command finished — and
    it says `succeeded`, so a reader treating a completed call as finished work is told so
    explicitly.

    Nothing folded says when the command actually ended: the `task_notification` reporting it,
    sixty frames later, is unprojected. A background command's end is in `session_frames` alone."""
    started = one(e for e in projection.events if isinstance(e, ToolCallStarted) and e.call_id == _BASH_CALL)
    completed = one(e for e in projection.events if isinstance(e, ToolCallCompleted) and e.item.call_id == _BASH_CALL)
    assert (started.tool_name, completed.outcome) == ("Bash", ToolOutcome.SUCCEEDED)

    ended = one(
        index
        for index, record in enumerate(_RECORDS)
        if record["frame"].get("subtype") == "task_notification" and record["frame"]["task_id"] == _BASH_TASK
    )
    assert isinstance(completed.provenance, FrameRange)
    assert completed.provenance.last_frame_seq < ended


def test_the_capture_holds_a_task_frame_for_each_task_type():
    """The evidence the fixture exists to hold, asserted off the records rather than the fold:
    `task_started` carries `tool_use_id` beside `task_id` on both the backgrounded shell and the
    subagent, so the link is on the wire for whatever reads it next."""
    assert {
        record["frame"]["task_id"]: record["frame"]["tool_use_id"]
        for record in _RECORDS
        if record.get("frame", {}).get("subtype") == "task_started"
    } == {_BASH_TASK: _BASH_CALL, _AGENT_TASK: _AGENT_CALL}


def test_the_subagent_is_a_tool_named_agent_whose_work_is_attributed_to_the_session(projection: Projection):
    """A subagent is the `Agent` tool — not `Task`, which is how `system/init` advertises it — and
    the frames it produces are folded as the session's own.

    Records 158 and 159 carry `parent_tool_use_id`, so the wire distinguishes the subagent's
    messages from the session's. `projection.py` never reads that field, so the fold does not: the
    subagent's message lands as an ordinary `MessageCompleted` with nothing marking it as nested."""
    started = next(e for e in projection.events if isinstance(e, ToolCallStarted) and e.call_id == _AGENT_CALL)
    assert started.tool_name == "Agent"

    nested = [
        index
        for index, record in enumerate(_RECORDS)
        if record.get("frame", {}).get("parent_tool_use_id") == _AGENT_CALL
    ]
    assert nested == [158, 159]

    # Every message is indistinguishable from every other, which is the finding.
    assert all(isinstance(e, MessageCompleted) for e in projection.events if isinstance(e, MessageCompleted))
    assert len([e for e in projection.events if isinstance(e, MessageCompleted)]) == 7


def test_no_folded_event_says_whether_the_subagent_succeeded(projection: Projection):
    """The `Agent` call completes with no outcome the CLI made explicit, and the `task_notification`
    that reported `completed` is unprojected — so a subagent's success is in the frames only. That
    is the accepted cost of the neutral vocabulary carrying no provider's task concept."""
    call = one(e for e in projection.events if isinstance(e, ToolCallCompleted) and e.item.call_id == _AGENT_CALL)
    assert call.outcome is ToolOutcome.UNKNOWN

    reported = one(
        record["frame"]
        for record in _RECORDS
        if record.get("frame", {}).get("subtype") == "task_notification" and record["frame"]["task_id"] == _AGENT_TASK
    )
    assert reported["status"] == "completed"


if __name__ == "__main__":
    pytest_bazel.main()
