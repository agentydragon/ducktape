"""What the projection does with the shapes production actually sends.

Every fixture here is built from <../../debug/frame_shape_census.md> — its block combinations, its
verbatim split-message sequence, its `tool_use_result` key sets, its undocumented frame classes —
rather than from what `protocol.md` says the wire looks like. Where the two disagree the census
is what the code has to survive, and each test below is named for the hazard it pins.

The shapes themselves come from <testing/wire.py>; what is written out here is a frame class no
release has seen, which is the one thing a builder cannot supply.
"""

from collections.abc import Iterable, Iterator, Sequence
from functools import reduce
from itertools import product

import pytest_bazel

from haku.console.chat_models import TurnOutcome
from haku.console.x.claude_code.projection import DeltaSource, RecordedFrame, finish, project, project_log
from haku.console.x.claude_code.testing.wire import (
    CENSUS_ACCOUNTING,
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
    ActivityCompleted,
    ActivityStarted,
    FrameRange,
    MessageCompleted,
    MessageKey,
    OpenMessage,
    Outcome,
    Projection,
    ProjectionState,
    Reasoning,
    TextContent,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    ToolReferences,
    TurnCompleted,
    Usage,
)

BASH_RESULT = {"interrupted": False, "isImage": False, "noOutputExpected": False, "stderr": "", "stdout": "3\n"}


def test_a_message_is_a_run_of_frames_not_a_frame():
    """Two frames, one `message.id`, no `stop_reason` — 47% of real messages look like this."""
    events = project_log(
        [
            recorded(1, assistant(thinking_block("The census says the fold is wrong here."), message_id="msg_A")),
            recorded(2, assistant(tool_use_block("toolu_1", "Bash", {"command": "ls"}), message_id="msg_A")),
            recorded(3, result(accounting=CENSUS_ACCOUNTING)),
        ]
    ).events

    assert events == (
        Reasoning(
            message=MessageKey(opened_at_frame_seq=1),
            summary="The census says the fold is wrong here.",
            provenance=FrameRange(1, 1),
        ),
        ToolCallStarted(
            message=MessageKey(opened_at_frame_seq=1),
            call_id="toolu_1",
            tool_name="Bash",
            arguments={"command": "ls"},
            provenance=FrameRange(2, 2),
        ),
        # One message, spanning both frames, and no text at all — 80% of real messages have none.
        MessageCompleted(
            message=MessageKey(opened_at_frame_seq=1), text=None, agent_message_id="msg_A", provenance=FrameRange(1, 2)
        ),
        TurnCompleted(
            outcome=TurnOutcome.ANSWERED,
            usage=Usage(
                input_tokens=19, output_tokens=1_204, cached_input_tokens=133_907, cost_usd=0.4213, duration_ms=41_902
            ),
            provenance=FrameRange(3, 3),
        ),
    )


def test_the_frames_of_one_message_are_not_always_contiguous():
    """The census's sequence verbatim: parallel calls, the first answered before the second is asked.

    Closing the message on the first non-`assistant` frame would make two messages out of one and
    attribute `toolu_2` to a message that does not exist.
    """
    events = project_log(
        [
            recorded(1, assistant(tool_use_block("toolu_1", "Read", {"file_path": "/a"}), message_id="msg_A")),
            recorded(
                2, tool_result("toolu_1", "1\tcontents\n", structured={"file": {"filePath": "/a"}, "type": "text"})
            ),
            recorded(3, assistant(tool_use_block("toolu_2", "Read", {"file_path": "/b"}), message_id="msg_A")),
        ]
    ).events

    one_message = MessageKey(opened_at_frame_seq=1)
    assert [type(event) for event in events] == [ToolCallStarted, ToolCallCompleted, ToolCallStarted, MessageCompleted]
    assert [event.message for event in events if isinstance(event, ToolCallStarted)] == [one_message, one_message]
    completed = events[3]
    assert isinstance(completed, MessageCompleted)
    # Inclusive of the tool result sitting inside it, which is what a range over the log means.
    assert completed.provenance == FrameRange(1, 3)
    assert completed.message == one_message


def test_the_tool_result_you_can_render_is_not_the_tool_result():
    """`content` is prose or names; the exit code, the patch and the MCP payload are elsewhere."""
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

    completions = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert completions[0].content == TextContent(text="3\n")
    assert completions[0].structured == BASH_RESULT
    # The 5.6% that a `content`-only model renders as empty: the blocks name tools and carry no
    # payload, and everything the call produced is in `structured`.
    assert completions[1].content == ToolReferences(tool_names=("Bash", "BashOutput"))
    assert completions[1].structured == deferred_search


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
            recorded(5, result(subtype="error_during_execution", accounting=CENSUS_ACCOUNTING)),
        ]
    ).events

    assert [event.outcome for event in events if isinstance(event, ToolCallCompleted)] == [
        Outcome.UNKNOWN,
        Outcome.SUCCEEDED,
        Outcome.FAILED,
    ]
    assert [event.outcome for event in events if isinstance(event, TurnCompleted)] == [TurnOutcome.FAILED]


def test_most_of_the_wire_is_system_and_projects_to_nothing():
    """73% of frames are `system` and 15% of those carry one constant. They cost a set lookup."""
    projection = project_log(
        [recorded(seq, heartbeat()) for seq in range(1, 40)]
        + [recorded(seq, system("status", status="working")) for seq in range(40, 80)]
        + [
            recorded(80, assistant(text_block("done"), message_id="msg_A")),
            recorded(81, result(accounting=CENSUS_ACCOUNTING)),
        ]
    )

    assert [type(event) for event in projection.events] == [TextDelta, MessageCompleted, TurnCompleted]
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
        recorded(6, result(accounting=CENSUS_ACCOUNTING)),
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


def test_activity_is_the_step_with_no_tool_name():
    """`task_started` and its terminal report pair by `task_id` and by nothing else."""
    events = project_log(
        [
            recorded(
                1,
                system(
                    "task_started",
                    task_id="task_9",
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
    ).events

    assert events == (
        ActivityStarted(activity_id="task_9", description="npm run build 2>&1 | tail -40", provenance=FrameRange(1, 1)),
        ActivityCompleted(
            activity_id="task_9", summary="Build finished", outcome=Outcome.SUCCEEDED, provenance=FrameRange(2, 2)
        ),
    )


def test_text_arrives_as_increments_and_as_a_finished_message():
    events = project_log(
        [
            recorded(1, assistant(text_block("Looking at "), message_id="msg_A")),
            recorded(2, assistant(text_block("the migration."), message_id="msg_A")),
            recorded(3, result(accounting=CENSUS_ACCOUNTING)),
        ]
    ).events

    assert events[:2] == (
        TextDelta(message=MessageKey(opened_at_frame_seq=1), text="Looking at ", provenance=FrameRange(1, 1)),
        TextDelta(message=MessageKey(opened_at_frame_seq=1), text="the migration.", provenance=FrameRange(2, 2)),
    )
    assert events[2] == MessageCompleted(
        message=MessageKey(opened_at_frame_seq=1),
        text="Looking at the migration.",
        agent_message_id="msg_A",
        provenance=FrameRange(1, 2),
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

    assert [type(event) for event in projection.events] == [TextDelta, MessageCompleted]
    assert projection.unprojected == {}
    # The one `TextDelta` is the completed block's, not the delta's.
    assert projection.events[0] == TextDelta(
        message=MessageKey(opened_at_frame_seq=2), text="Looking at the migration.", provenance=FrameRange(2, 2)
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
        TextDelta(message=MessageKey(opened_at_frame_seq=1), text="Looking at ", provenance=FrameRange(1, 1)),
        TextDelta(message=MessageKey(opened_at_frame_seq=2), text="the migration.", provenance=FrameRange(2, 2)),
        MessageCompleted(
            message=MessageKey(opened_at_frame_seq=3),
            text="Looking at the migration.",
            agent_message_id="msg_A",
            provenance=FrameRange(3, 3),
        ),
    )
    # Granularity is the only difference: the message a transcript keeps is the same either way.
    assert [event for event in live if isinstance(event, MessageCompleted)] == [
        event for event in project_log(frames).events if isinstance(event, MessageCompleted)
    ]


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
        recorded(12, result(accounting=CENSUS_ACCOUNTING)),
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

    assert [type(event) for event in first.events] == [TextDelta]
    # The message is in the state rather than in the events, and it is stated in the neutral
    # vocabulary — no `assistant`, no `msg_…` as identity, nothing a second backend could not
    # produce.
    assert state == ProjectionState(
        open_message=OpenMessage(
            key=MessageKey(opened_at_frame_seq=1), agent_message_id="msg_A", last_frame_seq=1, texts=("Looking at ",)
        )
    )

    state, second = project(state, [recorded(2, assistant(text_block("the migration."), message_id="msg_A"))])

    assert [type(event) for event in second.events] == [TextDelta]
    assert finish(state).events == (
        MessageCompleted(
            message=MessageKey(opened_at_frame_seq=1),
            text="Looking at the migration.",
            agent_message_id="msg_A",
            provenance=FrameRange(1, 2),
        ),
    )


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
