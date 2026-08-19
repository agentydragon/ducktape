"""What the projection does with the shapes production actually sends.

Every fixture here is built from <../../debug/frame_shape_census.md> — its block combinations, its
verbatim split-message sequence, its `tool_use_result` key sets, its undocumented frame classes —
rather than from what `protocol.md` says the wire looks like. Where the two disagree the census
is what the code has to survive, and each test below is named for the hazard it pins.

The shapes themselves come from <testing/wire.py>; what is written out here is a frame class no
release has seen, which is the one thing a builder cannot supply.
"""

import json
from collections.abc import Iterable, Iterator, Sequence
from functools import reduce
from itertools import product

import pytest
import pytest_bazel

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome
from haku.console.x.claude_code.projection import DeltaSource, RecordedFrame, finish, project, project_log, undelivered
from haku.console.x.claude_code.testing.wire import (
    assistant,
    command_lifecycle,
    heartbeat,
    input_json_delta,
    prompt,
    recorded,
    result,
    system,
    text_block,
    text_delta,
    thinking_block,
    tool_result,
    tool_use_block,
)
from haku.console.x.conversation_events import (
    CallRef,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenItem,
    OpenRef,
    Projection,
    ProjectionState,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)

BASH_RESULT = {"interrupted": False, "isImage": False, "noOutputExpected": False, "stderr": "", "stdout": "3\n"}

_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)
_REASONING = OpenRef(item_type=ItemType.REASONING)


def test_a_message_with_no_prose_in_it_is_not_a_message():
    """Two frames, one `message.id`, no `stop_reason` — 47% of real messages look like this.

    And 80% carry no text at all, which under a vocabulary where prose is the only thing an item
    holds means they open no message: what happened was a thought and a call, which are their own
    items and say so.
    """
    events = project_log(
        [
            recorded(1, assistant(thinking_block("The census says the fold is wrong here."), message_id="msg_A")),
            recorded(2, assistant(tool_use_block("toolu_1", "Bash", {"command": "ls"}), message_id="msg_A")),
            recorded(3, result()),
        ]
    ).events

    assert events == (
        ReasoningStarted(provenance=FrameRange(1, 1)),
        ItemSegment(item=_REASONING, text="The census says the fold is wrong here.", provenance=FrameRange(1, 1)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(1, 1)),
        ToolCallStarted(call_id="toolu_1", tool_name="Bash", arguments={"command": "ls"}, provenance=FrameRange(2, 2)),
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(3, 3)),
    )


def test_thinking_the_backend_will_not_show_you_is_an_item_with_no_prose():
    """`redacted_thinking`, which an empty summary string could not tell apart from silence."""
    events = project_log(
        [recorded(1, assistant({"type": "redacted_thinking", "data": "…"}, message_id="msg_A"))]
    ).events

    assert events == (
        ReasoningStarted(provenance=FrameRange(1, 1)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.WITHHELD, provenance=FrameRange(1, 1)),
    )


def test_the_frames_of_one_message_are_not_always_contiguous():
    """The census's sequence verbatim: parallel calls, the first answered before the second is asked.

    Closing the message on the first non-`assistant` frame would make two messages out of one and
    attribute `toolu_2` to a message that does not exist.

    What the message's span reports is the frames its **prose** was read from, which is what an
    operator appealing the folded text to the raw JSON needs. The calls are sibling items with spans
    of their own, so a frame that contributed no words to the message does not widen it.
    """
    events = project_log(
        [
            recorded(1, assistant(text_block("reading both"), message_id="msg_A")),
            recorded(2, assistant(tool_use_block("toolu_1", "Read", {"file_path": "/a"}), message_id="msg_A")),
            recorded(
                3, tool_result("toolu_1", "1\tcontents\n", structured={"file": {"filePath": "/a"}, "type": "text"})
            ),
            recorded(4, assistant(tool_use_block("toolu_2", "Read", {"file_path": "/b"}), message_id="msg_A")),
        ]
    ).events

    assert [type(event) for event in events] == [
        MessageStarted,
        ItemSegment,
        ToolCallStarted,
        ItemSegment,
        ToolCallCompleted,
        ToolCallStarted,
        MessageCompleted,
    ]
    completed = events[-1]
    assert isinstance(completed, MessageCompleted)
    assert completed.provenance == FrameRange(1, 1)
    assert completed.backend_item_id == "msg_A"


def test_the_tool_result_you_can_render_is_not_the_tool_result():
    """The showable half is segments like any other prose; the exit code and the MCP payload are not."""
    deferred_search = {"matches": [{"name": "Bash"}], "query": "shell", "total_deferred_tools": 112}
    events = project_log(
        [
            recorded(1, assistant(tool_use_block("toolu_1", "Bash", {"command": "ls | wc -l"}), message_id="msg_A")),
            recorded(2, tool_result("toolu_1", "3\n", structured=BASH_RESULT)),
            recorded(3, assistant(tool_use_block("toolu_2", "ToolSearch", {"query": "shell"}), message_id="msg_B")),
            recorded(
                4,
                tool_result(
                    "toolu_2",
                    [
                        {"tool_name": "Bash", "type": "tool_reference"},
                        {"tool_name": "BashOutput", "type": "tool_reference"},
                    ],
                    structured=deferred_search,
                ),
            ),
        ]
    ).events

    said = [event for event in events if isinstance(event, ItemSegment)]
    assert [(event.item, event.text) for event in said] == [
        (CallRef(call_id="toolu_1"), "3\n"),
        # The 5.6% that carry no prose at all: one harness's own block shape, rendered rather than
        # given a variant of its own, with everything the call produced in `structured`.
        (
            CallRef(call_id="toolu_2"),
            json.dumps(
                [{"tool_name": "Bash", "type": "tool_reference"}, {"tool_name": "BashOutput", "type": "tool_reference"}]
            ),
        ),
    ]
    completions = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert [event.structured for event in completions] == [BASH_RESULT, deferred_search]


def test_a_result_that_is_a_list_of_text_blocks_is_still_prose():
    """The shape an MCP tool's result arrives in, and the one list shape that is not JSON here."""
    events = project_log(
        [recorded(1, tool_result("toolu_1", [{"type": "text", "text": "one "}, {"type": "text", "text": "two"}]))]
    ).events

    assert [event.text for event in events if isinstance(event, ItemSegment)] == ["one two"]


def test_a_result_with_nothing_to_show_produces_no_segment():
    """An item's text is its segments, so a call that printed nothing must not carry an empty one."""
    events = project_log([recorded(1, tool_result("toolu_1", "", structured=BASH_RESULT))]).events

    assert [type(event) for event in events] == [ToolCallCompleted]


def test_every_did_this_go_wrong_field_is_uninformative():
    """Absent `is_error` is not `is_error: false`, and a turn's outcome is not read off one at all."""
    events = project_log(
        [
            recorded(1, assistant(tool_use_block("toolu_1", "Bash", {"command": "true"}), message_id="msg_A")),
            recorded(2, tool_result("toolu_1", "ok", structured=BASH_RESULT)),
            recorded(3, tool_result("toolu_2", "ok", structured=BASH_RESULT, is_error=False)),
            recorded(4, tool_result("toolu_3", "No such file", structured=BASH_RESULT, is_error=True)),
            # `subtype` is the CLI's own statement about the turn; `is_error` is false on this
            # frame as it is on every real one, and reading it would call this turn fine.
            recorded(5, result(subtype="error_during_execution")),
        ]
    ).events

    assert [event.outcome for event in events if isinstance(event, ToolCallCompleted)] == [
        ToolOutcome.UNKNOWN,
        ToolOutcome.SUCCEEDED,
        ToolOutcome.FAILED,
    ]
    assert [event.outcome for event in events if isinstance(event, TurnCompleted)] == [TurnOutcome.FAILED]


def test_most_of_the_wire_is_system_and_projects_to_nothing():
    """73% of frames are `system` and 15% of those carry one constant. They cost a set lookup."""
    projection = project_log(
        [recorded(seq, heartbeat()) for seq in range(1, 40)]
        + [recorded(seq, system("status", status="working")) for seq in range(40, 80)]
        + [recorded(80, assistant(text_block("done"), message_id="msg_A")), recorded(81, result())]
    )

    assert [type(event) for event in projection.events] == [
        MessageStarted,
        ItemSegment,
        MessageCompleted,
        TurnCompleted,
    ]
    # Deliberately ignored, so they are not noise in the signal that says the CLI grew a frame.
    assert projection.unprojected == {}


def test_the_default_branch_is_counted_rather_than_dropped_or_fatal():
    """Three frame classes and five `system` subtypes are undocumented, and the branch is routine."""
    projection = project_log(
        [
            RecordedFrame(1, {"type": "tool_progress", "tool_use_id": "toolu_1", "elapsed_time_seconds": 31}),
            RecordedFrame(2, {"type": "tool_progress", "tool_use_id": "toolu_1", "elapsed_time_seconds": 62}),
            recorded(3, system("vcs_state_changed", cwd="/w", kind="commit")),
            recorded(4, system("background_tasks_changed")),
            # A class no release has seen. The point of the branch is that this is what it does.
            RecordedFrame(5, {"type": "telepathy_event", "thought": "…"}),
            # The corpus's one `isSynthetic` frame: the CLI speaking as the user.
            RecordedFrame(
                6,
                {
                    "isSynthetic": True,
                    "message": {"content": [{"text": "No response requested.", "type": "text"}], "role": "user"},
                    "type": "user",
                },
            ),
        ]
    )

    assert projection.events == ()
    assert projection.unprojected == {
        "tool_progress": 2,
        "system/vcs_state_changed": 1,
        "system/background_tasks_changed": 1,
        "telepathy_event": 1,
        "user/text": 1,
    }


def test_command_lifecycle_is_not_a_clean_triple():
    """No `cancelled` ever, commands that start without queueing, commands that never complete,
    and `command_uuid`s matching no prompt the console sent — so nothing is derived from them."""
    conversation = [
        recorded(1, prompt("count the files")),
        recorded(3, assistant(text_block("3"), message_id="msg_A")),
        recorded(6, result()),
    ]
    lifecycle = [
        recorded(2, command_lifecycle("cmd_sent", "started")),  # no `queued` — 7 real commands begin here
        recorded(4, command_lifecycle("cmd_never_sent", "started")),  # a uuid the console never issued
        recorded(5, command_lifecycle("cmd_sent", "completed")),
        recorded(7, command_lifecycle("cmd_never_sent", "queued")),  # and one that never completes
    ]

    with_lifecycle = project_log(sorted(conversation + lifecycle, key=lambda frame: frame.frame_seq))
    assert with_lifecycle.events == project_log(conversation).events
    assert with_lifecycle.unprojected == {}


def test_a_background_task_says_nothing_to_the_neutral_vocabulary():
    """`task_started` and its terminal report are Claude's own concept — the harness's prose for a
    step in flight, keyed by identifiers no other backend has — so they are counted rather than
    projected. The frames stay in `session_frames` for anyone who wants them back."""
    projection = project_log(
        [
            recorded(
                1,
                system(
                    "task_started",
                    task_id="task_9",
                    tool_use_id="toolu_9",
                    task_type="local_bash",
                    description="npm run build 2>&1 | tail -40",
                ),
            ),
            recorded(
                2,
                system(
                    "task_notification",
                    task_id="task_9",
                    status="completed",
                    summary="Build finished",
                    output_file="/tmp/o",
                ),
            ),
        ]
    )

    assert projection.events == ()
    assert projection.unprojected == {"system/task_started": 1, "system/task_notification": 1}


def test_text_arrives_as_segments_and_the_completion_carries_none():
    """The invariant the whole vocabulary rests on: an item's text is exactly its segments."""
    events = project_log(
        [
            recorded(1, assistant(text_block("Looking at "), message_id="msg_A")),
            recorded(2, assistant(text_block("the migration."), message_id="msg_A")),
            recorded(3, result()),
        ]
    ).events

    assert events == (
        MessageStarted(provenance=FrameRange(1, 1)),
        ItemSegment(item=_MESSAGE, text="Looking at ", provenance=FrameRange(1, 1)),
        ItemSegment(item=_MESSAGE, text="the migration.", provenance=FrameRange(2, 2)),
        MessageCompleted(backend_item_id="msg_A", provenance=FrameRange(1, 2)),
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(3, 3)),
    )


def test_deltas_are_not_what_text_is_projected_from():
    """`stream_event` occurs in 4 of 28 sessions and is mostly tool arguments, so a consumer built
    on it would render nothing on the other 24."""
    projection = project_log(
        [
            recorded(1, text_delta("Look")),
            recorded(2, assistant(text_block("Looking at the migration."), message_id="msg_A")),
        ]
    )

    assert [type(event) for event in projection.events] == [MessageStarted, ItemSegment, MessageCompleted]
    assert projection.unprojected == {}
    # The one segment is the completed block's, not the delta's — the delta's frame projects to
    # nothing under this `DeltaSource`.
    assert projection.events[1] == ItemSegment(
        item=_MESSAGE, text="Looking at the migration.", provenance=FrameRange(2, 2)
    )


def test_a_live_consumer_cuts_the_same_prose_where_the_wire_cut_it():
    """The other `DeltaSource`, which the turn loop drives: an answer becomes visible as it is
    written, and the completed block does not repeat prose the deltas already delivered."""
    frames = [
        recorded(1, text_delta("Looking at ")),
        recorded(2, text_delta("the migration.")),
        recorded(3, assistant(text_block("Looking at the migration."), message_id="msg_A")),
    ]

    live = project_log(frames, delta_source=DeltaSource.STREAM_EVENTS).events

    assert live == (
        MessageStarted(provenance=FrameRange(1, 1)),
        ItemSegment(item=_MESSAGE, text="Looking at ", provenance=FrameRange(1, 1)),
        ItemSegment(item=_MESSAGE, text="the migration.", provenance=FrameRange(2, 2)),
        # The completed block joins the message the deltas opened rather than starting a second —
        # which under this `DeltaSource` would be an empty one, since the deltas already delivered
        # every word — and gives it the id the wire had not supplied yet.
        MessageCompleted(backend_item_id="msg_A", provenance=FrameRange(1, 3)),
    )
    # Granularity is the only difference: the prose a transcript keeps is the same either way.
    assert "".join(event.text for event in live if isinstance(event, ItemSegment)) == "".join(
        event.text for event in project_log(frames).events if isinstance(event, ItemSegment)
    )


@pytest.mark.parametrize(
    ("text", "delivered", "expected"),
    [
        # One process folding the whole message: the delivered prose is this block's own prefix.
        pytest.param("Looking at the migration.", "Looking at ", "the migration.", id="mid-stream"),
        # A backend that streams nothing has delivered nothing, so the block is emitted whole.
        pytest.param("Looking at the migration.", "", "Looking at the migration.", id="no-deltas"),
        # A fold resuming an item another process left open inherits that item's prose entire, of
        # which only a suffix belongs to the block now arriving.
        pytest.param("the migration.", "Looking at ", "the migration.", id="inherited-earlier-block"),
        pytest.param("Looking at the migration.", "First. Looking at ", "the migration.", id="inherited-and-streaming"),
    ],
)
def test_a_completed_block_is_emitted_minus_what_was_already_said(text: str, delivered: str, expected: str) -> None:
    """The completed block repeats its deltas, so emitting it whole would say the answer twice — and
    subtracting a length rather than the overlap would eat the answer of a resumed message whose
    inherited prose came from an earlier block."""
    assert undelivered(text, delivered) == expected


def test_a_live_consumer_is_not_shown_tool_arguments_as_prose():
    """87 of 950 production deltas were text; the rest are `input_json_delta`, and a consumer that
    read them as an answer would render a half-typed JSON blob into the room."""
    arguments = recorded(1, input_json_delta('{"fi'))

    assert project_log([arguments], delta_source=DeltaSource.STREAM_EVENTS).events == ()


def census_session() -> list[RecordedFrame]:
    """A whole exchange with every hazard in it: an interrupted message, an unreadable frame,
    ignored classes, a second message, and a turn that ends."""
    return [
        recorded(1, prompt("split the migration")),
        recorded(2, command_lifecycle("cmd_1", "queued")),
        recorded(3, heartbeat()),
        recorded(4, assistant(thinking_block("Two revisions share an id."), message_id="msg_A")),
        recorded(5, assistant(tool_use_block("toolu_1", "Read", {"file_path": "/m/0043.py"}), message_id="msg_A")),
        recorded(
            6,
            tool_result(
                "toolu_1", "1\tdef upgrade():\n", structured={"file": {"filePath": "/m/0043.py"}, "type": "text"}
            ),
        ),
        recorded(7, assistant(tool_use_block("toolu_2", "Bash", {"command": "ls migrations"}), message_id="msg_A")),
        recorded(8, tool_result("toolu_2", "0043.py\n", structured=BASH_RESULT, is_error=False)),
        recorded(9, system("vcs_state_changed", cwd="/w", kind="commit")),
        recorded(10, assistant(text_block("Split, "), message_id="msg_B")),
        recorded(11, assistant(text_block("and the second one now runs."), message_id="msg_B")),
        recorded(12, result()),
    ]


def test_reprojection_reproduces_the_same_events():
    """The anti-drift property: stored frames re-project to what was stored, or the comparison
    that detects drift is itself the thing drifting."""
    session = census_session()

    first, second = project_log(session), project_log(session)
    assert first == second
    # And a projection of the same frames read again, not the same objects folded twice.
    assert project_log([RecordedFrame(frame.frame_seq, dict(frame.payload)) for frame in session]) == first


def test_a_batch_running_out_is_not_a_message_ending():
    """The difference between a reducer and a fold over a whole log: the next batch may continue
    the message, so only a caller saying the stream is over may close it."""
    state, first = project(ProjectionState(), [recorded(1, assistant(text_block("Looking at "), message_id="msg_A"))])

    assert [type(event) for event in first.events] == [MessageStarted, ItemSegment]
    # The message is in the state rather than in the events, and it holds no prose: the segments
    # were emitted as they arrived, so nothing is carried across a batch boundary to be joined.
    assert state == ProjectionState(
        open_message=OpenItem(opened_at_frame_seq=1, last_frame_seq=1, backend_item_id="msg_A")
    )

    state, second = project(state, [recorded(2, assistant(text_block("the migration."), message_id="msg_A"))])

    assert [type(event) for event in second.events] == [ItemSegment]
    assert finish(state).events == (MessageCompleted(backend_item_id="msg_A", provenance=FrameRange(1, 2)),)


def _in_batches(batches: Iterable[Sequence[RecordedFrame]]) -> Projection:
    state = ProjectionState()
    projected = []
    for batch in batches:
        state, batch_projection = project(state, batch)
        projected.append(batch_projection)
    projected.append(finish(state))
    return reduce(Projection.then, projected)


def _splits(frames: Sequence[RecordedFrame]) -> Iterator[list[Sequence[RecordedFrame]]]:
    """Every way of cutting the sequence into consecutive batches — one batch of everything, one
    batch per frame, and each of the 2**(n-1) arrangements between."""
    for cuts in product([False, True], repeat=len(frames) - 1):
        batches: list[Sequence[RecordedFrame]] = [[frames[0]]]
        for cut, frame in zip(cuts, frames[1:], strict=True):
            if cut:
                batches.append([frame])
            else:
                batches[-1] = [*batches[-1], frame]
        yield batches


def test_one_batch_and_any_split_of_batches_project_alike():
    """What makes "project each frame as it lands" and "project from the stored cursor, which
    happens to be behind" the same code path — checked over every split rather than a chosen one,
    since a live consumer's batches are whatever the socket handed it."""
    session = census_session()
    whole = project_log(session)

    assert all(_in_batches(batches) == whole for batches in _splits(session))
    # And the extreme the property exists for: one frame at a time, which is what a live
    # consumer does.
    assert _in_batches([[frame] for frame in session]) == whole


if __name__ == "__main__":
    pytest_bazel.main()
