"""What the projection does with the shapes production actually sends.

Every fixture here is built from <../../debug/frame_shape_census.md> — its block combinations, its
verbatim split-message sequence, its `tool_use_result` key sets, its undocumented frame classes —
rather than from what `protocol.md` says the wire looks like. Where the two disagree the census
is what the code has to survive, and each test below is named for the hazard it pins.
"""

from collections.abc import Iterable, Iterator, Sequence
from functools import reduce
from itertools import product
from typing import Any

import pytest_bazel

from haku.console.chat_models import TurnOutcome
from haku.console.x.claude_code.projection import DeltaSource, RecordedFrame, finish, project, project_log
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

# Every wire `assistant` frame carries these keys and no others, so the fixtures carry them too:
# what the projection ignores is as much a fact about it as what it reads.
ASSISTANT_USAGE = {
    "cache_creation": {"ephemeral_1h_input_tokens": 0},
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 21_507,
    "inference_geo": "us",
    "input_tokens": 4,
    "output_tokens": 91,
    "service_tier": "standard",
}


def assistant(frame_seq: int, message_id: str, block: dict[str, Any]) -> RecordedFrame:
    """One `assistant` frame carrying exactly one content block, which is all any of them carry."""
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "message": {
                "content": [block],
                "context_management": None,
                "diagnostics": None,
                "id": message_id,
                "model": "claude-opus-4-6-20260514",
                "role": "assistant",
                "stop_details": None,
                # Null on all 1,887 production frames: nothing in an `assistant` frame says the
                # message is finished.
                "stop_reason": None,
                "stop_sequence": None,
                "type": "message",
                "usage": ASSISTANT_USAGE,
            },
            "parent_tool_use_id": None,
            "request_id": "req_011CX",
            "session_id": "a2d5",
            "timestamp": "2026-08-15T06:12:04.113Z",
            "type": "assistant",
            "uuid": f"uuid-{frame_seq}",
        },
    )


def text_block(text: str) -> dict[str, Any]:
    return {"text": text, "type": "text"}


def thinking_block(thinking: str) -> dict[str, Any]:
    return {"signature": "EqQBCkYIBxgCK", "thinking": thinking, "type": "thinking"}


def tool_use_block(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"caller": {"type": "direct"}, "id": call_id, "input": arguments, "name": name, "type": "tool_use"}


def tool_result(
    frame_seq: int, call_id: str, content: Any, structured: Any, *, is_error: bool | None = None
) -> RecordedFrame:
    """An inbound `user` frame. `is_error=None` is the 56% of results that omit the key entirely."""
    block: dict[str, Any] = {"content": content, "tool_use_id": call_id, "type": "tool_result"}
    if is_error is not None:
        block["is_error"] = is_error
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "message": {"content": [block], "role": "user"},
            "parent_tool_use_id": None,
            "session_id": "a2d5",
            "tool_use_result": structured,
            "type": "user",
            "uuid": f"uuid-{frame_seq}",
        },
    )


def prompt(frame_seq: int, text: str) -> RecordedFrame:
    """An outbound prompt: content is a string on 121 of 121, which is what says which way it went."""
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "message": {"content": text, "role": "user"},
            "parent_tool_use_id": None,
            "type": "user",
            "uuid": f"uuid-{frame_seq}",
        },
    )


def text_delta_frame(frame_seq: int, text: str) -> RecordedFrame:
    return RecordedFrame(
        frame_seq,
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        },
    )


def result(frame_seq: int, *, subtype: str = "success") -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "api_error_status": None,
            "duration_ms": 41_902,
            "duration_api_ms": 41_388,
            # False on all 129 production results, including 27 sessions the console failed.
            "is_error": False,
            "num_turns": 7,
            "permission_denials": [],
            "result": "Done — the migration is split in two.",
            "session_id": "a2d5",
            "stop_reason": "end_turn",
            "subtype": subtype,
            "terminal_reason": "completed",
            "total_cost_usd": 0.4213,
            "type": "result",
            "usage": {
                "cache_creation_input_tokens": 1_882,
                "cache_read_input_tokens": 133_907,
                "input_tokens": 19,
                "iterations": 7,
                "output_tokens": 1_204,
                "service_tier": "standard",
            },
            "uuid": f"uuid-{frame_seq}",
        },
    )


def system(frame_seq: int, subtype: str, **fields: Any) -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={"session_id": "a2d5", "subtype": subtype, "type": "system", "uuid": f"uuid-{frame_seq}"} | fields,
    )


def heartbeat(frame_seq: int) -> RecordedFrame:
    """`thinking_tokens` — 8,512 frames, 56% of everything recorded."""
    return system(frame_seq, "thinking_tokens", estimated_tokens=1_024, estimated_tokens_delta=31)


def command_lifecycle(frame_seq: int, command_uuid: str, state: str) -> RecordedFrame:
    return RecordedFrame(
        frame_seq=frame_seq,
        payload={
            "command_uuid": command_uuid,
            "session_id": "a2d5",
            "state": state,
            "type": "command_lifecycle",
            "uuid": f"uuid-{frame_seq}",
        },
    )


BASH_RESULT = {"interrupted": False, "isImage": False, "noOutputExpected": False, "stderr": "", "stdout": "3\n"}


def test_a_message_is_a_run_of_frames_not_a_frame():
    """Two frames, one `message.id`, no `stop_reason` — 47% of real messages look like this."""
    events = project_log(
        [
            assistant(1, "msg_A", thinking_block("The census says the fold is wrong here.")),
            assistant(2, "msg_A", tool_use_block("toolu_1", "Bash", {"command": "ls"})),
            result(3),
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
            assistant(1, "msg_A", tool_use_block("toolu_1", "Read", {"file_path": "/a"})),
            tool_result(2, "toolu_1", "1\tcontents\n", {"file": {"filePath": "/a"}, "type": "text"}),
            assistant(3, "msg_A", tool_use_block("toolu_2", "Read", {"file_path": "/b"})),
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
            assistant(1, "msg_A", tool_use_block("toolu_1", "Bash", {"command": "ls | wc -l"})),
            tool_result(2, "toolu_1", "3\n", BASH_RESULT),
            assistant(3, "msg_B", tool_use_block("toolu_2", "ToolSearch", {"query": "shell"})),
            tool_result(
                4,
                "toolu_2",
                [
                    {"tool_name": "Bash", "type": "tool_reference"},
                    {"tool_name": "BashOutput", "type": "tool_reference"},
                ],
                deferred_search,
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
            assistant(1, "msg_A", tool_use_block("toolu_1", "Bash", {"command": "true"})),
            tool_result(2, "toolu_1", "ok", BASH_RESULT),
            tool_result(3, "toolu_2", "ok", BASH_RESULT, is_error=False),
            tool_result(4, "toolu_3", "No such file", BASH_RESULT, is_error=True),
            # `subtype` is the CLI's own statement about the turn; `is_error` is false on this
            # frame as it is on every real one, and reading it would call this turn fine.
            result(5, subtype="error_during_execution"),
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
        [heartbeat(seq) for seq in range(1, 40)]
        + [system(seq, "status", status="working") for seq in range(40, 80)]
        + [assistant(80, "msg_A", text_block("done")), result(81)]
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
            system(3, "vcs_state_changed", cwd="/w", kind="commit"),
            system(4, "background_tasks_changed"),
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
    conversation = [prompt(1, "count the files"), assistant(3, "msg_A", text_block("3")), result(6)]
    lifecycle = [
        command_lifecycle(2, "cmd_sent", "started"),  # no `queued` — 7 real commands begin here
        command_lifecycle(4, "cmd_never_sent", "started"),  # a uuid the console never issued
        command_lifecycle(5, "cmd_sent", "completed"),
        command_lifecycle(7, "cmd_never_sent", "queued"),  # and one that never completes
    ]

    with_lifecycle = project_log(sorted(conversation + lifecycle, key=lambda frame: frame.frame_seq))
    assert with_lifecycle.events == project_log(conversation).events
    assert with_lifecycle.unprojected == {}


def test_activity_is_the_step_with_no_tool_name():
    """`task_started` and its terminal report pair by `task_id` and by nothing else."""
    events = project_log(
        [
            system(
                1, "task_started", task_id="task_9", task_type="local_bash", description="npm run build 2>&1 | tail -40"
            ),
            system(
                2,
                "task_notification",
                task_id="task_9",
                status="completed",
                summary="Build finished",
                output_file="/tmp/o",
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
            assistant(1, "msg_A", text_block("Looking at ")),
            assistant(2, "msg_A", text_block("the migration.")),
            result(3),
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
        [text_delta_frame(1, "Look"), assistant(2, "msg_A", text_block("Looking at the migration."))]
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
        text_delta_frame(1, "Looking at "),
        text_delta_frame(2, "the migration."),
        assistant(3, "msg_A", text_block("Looking at the migration.")),
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
    arguments = RecordedFrame(
        1,
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"fi'},
            },
        },
    )

    assert project_log([arguments], delta_source=DeltaSource.STREAM_EVENTS).events == ()


def census_session() -> list[RecordedFrame]:
    """A whole exchange with every hazard in it: an interrupted message, an unreadable frame,
    ignored classes, a second message, and a turn that ends."""
    return [
        prompt(1, "split the migration"),
        command_lifecycle(2, "cmd_1", "queued"),
        heartbeat(3),
        assistant(4, "msg_A", thinking_block("Two revisions share an id.")),
        assistant(5, "msg_A", tool_use_block("toolu_1", "Read", {"file_path": "/m/0043.py"})),
        tool_result(6, "toolu_1", "1\tdef upgrade():\n", {"file": {"filePath": "/m/0043.py"}, "type": "text"}),
        assistant(7, "msg_A", tool_use_block("toolu_2", "Bash", {"command": "ls migrations"})),
        tool_result(8, "toolu_2", "0043.py\n", BASH_RESULT, is_error=False),
        system(9, "vcs_state_changed", cwd="/w", kind="commit"),
        assistant(10, "msg_B", text_block("Split, ")),
        assistant(11, "msg_B", text_block("and the second one now runs.")),
        result(12),
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
    state, first = project(ProjectionState(), [assistant(1, "msg_A", text_block("Looking at "))])

    assert [type(event) for event in first.events] == [TextDelta]
    # The message is in the state rather than in the events, and it is stated in the neutral
    # vocabulary — no `assistant`, no `msg_…` as identity, nothing a second backend could not
    # produce.
    assert state == ProjectionState(
        open_message=OpenMessage(
            key=MessageKey(opened_at_frame_seq=1), agent_message_id="msg_A", last_frame_seq=1, texts=("Looking at ",)
        )
    )

    state, second = project(state, [assistant(2, "msg_A", text_block("the migration."))])

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
