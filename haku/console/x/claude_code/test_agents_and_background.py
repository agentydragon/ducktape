"""What the projection does with a subagent and a backgrounded command, taken off the wire.

<test_diverse_session.py>'s capture holds one straight-line session: text, thinking, tool calls,
one failure. This one holds the shapes that capture has none of — a command the CLI ran in the
background and reported on later, and a **subagent**, whose frames carry a `parent_tool_use_id`
naming the call that spawned it (`testdata/agents_and_background.jsonl`, redacted; provenance in
<../../debug/frame_shape_census.md> § A capture of agents and background work).

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

from haku.console.chat_models import TurnOutcome
from haku.console.x.claude_code.projection import RecordedFrame, project_log
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    MessageCompleted,
    Outcome,
    Projection,
    Reasoning,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
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
    return project_log(_FRAMES)


def test_the_capture_folds_to_two_turns_with_two_background_activities(projection: Projection):
    """Two turns, because the CLI re-initialised mid-capture (a second `system/init` at record 185)
    rather than because the subagent's completion closed anything — both `result` frames have a null
    `parent_tool_use_id`."""
    assert Counter(type(event) for event in projection.events) == {
        Reasoning: 7,
        TextDelta: 7,
        MessageCompleted: 7,
        ToolCallStarted: 4,
        ToolCallCompleted: 4,
        ActivityStarted: 2,
        ActivityCompleted: 2,
        TurnCompleted: 2,
    }
    assert [event.outcome for event in projection.events if isinstance(event, TurnCompleted)] == [
        TurnOutcome.ANSWERED,
        TurnOutcome.ANSWERED,
    ]


def test_background_and_subagent_frame_classes_are_unprojected(projection: Projection):
    """`background_tasks_changed` and `task_updated` reach the fold's default branch.

    Both are the CLI's own bookkeeping — the live task list, and a patch carrying a task's terminal
    status — and both are new here: neither appears in `test_diverse_session.py`'s capture. Named so
    that a release which starts carrying something the projection needs fails here rather than
    silently incrementing a counter."""
    assert projection.unprojected == {"system/background_tasks_changed": 4, "system/task_updated": 2}


def test_a_backgrounded_call_completes_while_its_command_still_runs(projection: Projection):
    """The `Bash` call is answered long before the command it started finishes.

    `ToolCallCompleted` is the call returning a shell id, not the command ending; the command's end
    is the `ActivityCompleted` two messages later. Anything reading tool-call completion as "the
    work is done" is wrong for a backgrounded call, and this is the fixture that says so."""
    order = [
        index
        for index, event in enumerate(projection.events)
        if (isinstance(event, ToolCallCompleted) and event.call_id == _BASH_CALL)
        or (isinstance(event, ActivityCompleted) and event.activity_id == _BASH_TASK)
    ]
    call_completed, activity_completed = order
    assert call_completed < activity_completed

    started = [e for e in projection.events if isinstance(e, ToolCallStarted) and e.call_id == _BASH_CALL]
    assert [e.tool_name for e in started] == ["Bash"]


def test_the_activity_carries_the_call_the_frame_named(projection: Projection):
    """**The link on the wire survives the fold.**

    `task_started` carries both `task_id` and `tool_use_id`, so the frame says exactly which call
    opened which background task, and `ActivityStarted` now keeps both — which is what lets a
    reader pair the activity that reports the command's end with the `Bash` call that started it.
    Asserting the frame half too, and for both task types the capture holds — the backgrounded
    shell and the subagent — because those two frames are the whole of the evidence that
    `tool_use_id` is always there, which is what makes `call_id` a required field rather than a
    nullable one."""
    frames = {
        record["frame"]["task_id"]: record["frame"]["tool_use_id"]
        for record in _RECORDS
        if record.get("frame", {}).get("subtype") == "task_started"
    }
    assert frames == {_BASH_TASK: _BASH_CALL, _AGENT_TASK: _AGENT_CALL}

    activities = {e.activity_id: e.call_id for e in projection.events if isinstance(e, ActivityStarted)}
    assert activities == frames


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


def test_the_subagent_activity_reports_success_but_its_call_outcome_is_unknown(projection: Projection):
    """The `Agent` call's own completion carries no outcome the CLI made explicit, while the task
    behind it reports `completed`. So what tells you a subagent succeeded is the activity, not the
    tool call — the opposite of the `Bash` case above, where the call is what carries it."""
    call = next(e for e in projection.events if isinstance(e, ToolCallCompleted) and e.call_id == _AGENT_CALL)
    activity = next(e for e in projection.events if isinstance(e, ActivityCompleted) and e.activity_id == _AGENT_TASK)
    assert call.outcome is Outcome.UNKNOWN
    assert activity.outcome is Outcome.SUCCEEDED


if __name__ == "__main__":
    pytest_bazel.main()
