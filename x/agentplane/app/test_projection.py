"""Parity and semantics of the derived-view fold.

The load-bearing test is `test_batches_reach_the_same_state_as_a_whole_fold`: whatever boundaries a
projector transaction happens to commit on, a client applying those batches must land on exactly the
state a rebuild would produce. Everything else pins a case from the design's acceptance matrix.
"""

import random

import pytest
import pytest_bazel

from x.agentplane.app.projection import (
    EMPTY,
    CommandReceipt,
    ConfirmedInput,
    Effected,
    Failed,
    HarnessState,
    Item,
    Lifecycle,
    ModelEffect,
    Noop,
    OperationKind,
    Pending,
    Projection,
    SegmentContent,
    TextOutcome,
    ToolOutcome,
    Turn,
    TurnOutcome,
    UninterpretedObservationError,
    advance,
    apply_batch,
    project,
)
from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf


def entry(cursor: int, event: event_pb2.Event) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(cursor=cursor, event=event)


def admitted(cursor: int, command: command_pb2.Command) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))


def contents(projection: Projection) -> list[SegmentContent]:
    return [segment.content for segment in projection.segments]


def by_anchor(projection: Projection) -> dict[int, SegmentContent]:
    """Segments keyed the way the contract keys them, rather than by position in the tuple."""
    return {segment.anchor_cursor: segment.content for segment in projection.segments}


INPUT = command_pb2.Command(command_id="c-input", submit_input=command_pb2.SubmitInput(text="inspect this"))
MODEL = command_pb2.Command(command_id="c-model", change_model=command_pb2.ChangeModel(model="next-model"))
INTERRUPT = command_pb2.Command(command_id="c-stop", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn-1"))


@pytest.fixture
def script() -> list[event_log_pb2.EventEntry]:
    """One turn's worth of every observation the fold models, in a plausible order."""
    return [
        entry(1, event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=42))),
        admitted(2, INPUT),
        entry(
            3,
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="m-1", text="inspect this", origin_command_ids=["c-input"], turn_id="turn-1"
                )
            ),
        ),
        entry(4, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="first-model"))),
        entry(5, event_pb2.Event(native=event_pb2.Native(line="{}"))),
        entry(
            6, event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="i-1", kind=event_pb2.ITEM_KIND_REASONING))
        ),
        entry(7, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="i-1", text="think"))),
        entry(8, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="i-1", text="ing"))),
        entry(9, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="i-1", text="thinking"))),
        entry(
            10,
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(
                    item_id="i-2", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="read_file"
                )
            ),
        ),
        entry(
            11, event_pb2.Event(tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="i-2", partial_json='{"pa'))
        ),
        entry(
            12, event_pb2.Event(tool_arguments=event_pb2.ToolArguments(item_id="i-2", arguments_json='{"path": "a"}'))
        ),
        entry(13, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="i-2", text="partial"))),
        entry(
            14,
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id="i-2", tool=event_pb2.ToolResult(output="whole output", succeeded=True)
                )
            ),
        ),
        admitted(15, MODEL),
        entry(16, event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="a warning"))),
        entry(
            17,
            event_pb2.Event(
                model_changed=event_pb2.ModelChanged(
                    command_id="c-model", previous_model="first-model", model="next-model"
                )
            ),
        ),
        entry(
            18,
            event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="i-3", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
        ),
        entry(19, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="i-3", text="done"))),
        entry(20, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="i-3", text="done"))),
        entry(
            21,
            event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id="turn-1", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        ),
        entry(22, event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=0))),
    ]


def replay(entries: list[event_log_pb2.EventEntry], boundaries: list[int]) -> Projection:
    """Fold in the given batch sizes on the server and apply each batch on the client."""
    server, client = EMPTY, EMPTY
    start = 0
    for size in boundaries:
        server, batch = advance(server, entries[start : start + size])
        client = apply_batch(client, batch)
        start += size
    assert start == len(entries)
    return client


def test_batches_reach_the_same_state_as_a_whole_fold(script: list[event_log_pb2.EventEntry]) -> None:
    whole = project(script)
    for split in range(len(script) + 1):
        assert replay(script, [split, len(script) - split]) == whole, f"{split=}"

    generator = random.Random(20260916)
    for _ in range(200):
        boundaries, remaining = [], len(script)
        while remaining:
            size = generator.randint(1, remaining)
            boundaries.append(size)
            remaining -= size
        assert replay(script, boundaries) == whole, f"{boundaries=}"


def test_a_batch_carries_only_what_changed_in_its_interval(script: list[event_log_pb2.EventEntry]) -> None:
    before, _ = advance(EMPTY, script[:10])
    _, batch = advance(before, script[10:14])

    assert batch.after_cursor == 10
    assert batch.through_cursor == 14
    # Only the tool item, revised four times in this interval, and nothing from the completed turn.
    assert [segment.anchor_cursor for segment in batch.segments] == [10]
    assert batch.segments[0].revision_cursor == 14


def test_a_native_only_interval_advances_coverage_without_inventing_segments(
    script: list[event_log_pb2.EventEntry],
) -> None:
    before, _ = advance(EMPTY, script[:4])
    after, batch = advance(
        before, [entry(cursor, event_pb2.Event(native=event_pb2.Native(line="{}"))) for cursor in (5, 6, 7)]
    )

    assert after.through_cursor == 7
    assert batch.segments == ()
    assert after.segments == before.segments


def test_streaming_text_is_replaced_by_the_authoritative_completion(script: list[event_log_pb2.EventEntry]) -> None:
    item = by_anchor(project(script))[6]
    assert isinstance(item, Item)
    assert item.item_id == "i-1"
    assert item.text == "thinking"
    assert item.completion == TextOutcome(text="thinking")


def test_an_incomplete_item_carries_no_completion(script: list[event_log_pb2.EventEntry]) -> None:
    streaming = project(script[:13]).segments
    tool = streaming[-1].content
    assert isinstance(tool, Item)
    assert tool.output == "partial"
    assert tool.completion is None


def test_a_completed_tool_keeps_its_result(script: list[event_log_pb2.EventEntry]) -> None:
    tool = by_anchor(project(script))[10]
    assert isinstance(tool, Item)
    assert tool.item_id == "i-2"
    assert tool.completion == ToolOutcome(output="whole output", succeeded=True)
    assert tool.arguments_json == '{"path": "a"}'


def test_an_item_anchors_at_its_first_mention_not_its_start_event() -> None:
    """A delta can arrive before `ItemStarted`; the Segment still belongs at the earlier cursor."""
    projected = project(
        [
            entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="i-1", text="early"))),
            entry(
                2,
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id="i-1", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                ),
            ),
        ]
    )
    (segment,) = projected.segments
    assert segment.anchor_cursor == 1
    assert segment.revision_cursor == 2
    assert segment.content == Item(
        item_id="i-1",
        kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT,
        tool_name="",
        text="early",
        arguments_json="",
        output="",
        completion=None,
    )


def test_a_turn_outcome_anchors_at_the_terminal_event_after_partial_output(
    script: list[event_log_pb2.EventEntry],
) -> None:
    projected = project(script)
    anchors = by_anchor(projected)
    turn = anchors[4]
    assert isinstance(turn, Turn)
    # The turn Segment records its status, and the outcome still gets its own Segment at the end.
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert anchors[21] == TurnOutcome(
        turn_id="turn-1", status=event_pb2.TURN_STATUS_COMPLETED, error="", interrupted_by_command_id=""
    )


def test_a_failed_turn_without_output_stays_visible() -> None:
    projected = project(
        [
            entry(1, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-9", model="m"))),
            entry(
                2,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="turn-9", status=event_pb2.TURN_STATUS_FAILED, error="upstream 502"
                    )
                ),
            ),
        ]
    )
    outcome = projected.segments[-1].content
    assert outcome == TurnOutcome(
        turn_id="turn-9", status=event_pb2.TURN_STATUS_FAILED, error="upstream 502", interrupted_by_command_id=""
    )
    assert projected.controls.active_turn_id is None


def test_coalesced_input_keeps_every_origin_command() -> None:
    """Claude merges queued inputs into one confirmation; all their ids survive, at confirmation."""
    second = command_pb2.Command(command_id="c-2", submit_input=command_pb2.SubmitInput(text="and this"))
    projected = project(
        [
            admitted(1, INPUT),
            admitted(2, second),
            entry(
                3,
                event_pb2.Event(
                    harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                        harness_message_id="m-1",
                        text="inspect this\nand this",
                        origin_command_ids=["c-input", "c-2"],
                        turn_id="turn-1",
                    )
                ),
            ),
        ]
    )
    confirmed = projected.segments[-1].content
    assert confirmed == ConfirmedInput(
        harness_message_id="m-1", text="inspect this\nand this", turn_id="turn-1", origin_command_ids=("c-input", "c-2")
    )
    assert projected.segments[-1].anchor_cursor == 3
    assert [summary.outcome for summary in projected.commands] == [Effected(3), Effected(3)]


def test_a_queued_model_change_stays_pending_until_its_effect(script: list[event_log_pb2.EventEntry]) -> None:
    queued = project(script[:16])
    assert [(s.command_id, s.outcome) for s in queued.commands if s.command_id == "c-model"] == [("c-model", Pending())]
    # The picker must not move on admission alone.
    assert queued.controls.applied_model == "first-model"

    effected = project(script[:17])
    assert [s.outcome for s in effected.commands if s.command_id == "c-model"] == [Effected(17)]
    assert effected.controls.applied_model == "next-model"


def test_an_interrupt_settles_against_the_turn_it_named() -> None:
    projected = project(
        [
            entry(1, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="m"))),
            admitted(2, INTERRUPT),
            entry(
                3,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="turn-1", status=event_pb2.TURN_STATUS_INTERRUPTED, interrupted_by_command_id="c-stop"
                    )
                ),
            ),
        ]
    )
    (summary,) = projected.commands
    assert summary.operation_kind == OperationKind.INTERRUPT_TURN
    assert summary.outcome == Effected(3)
    outcome = projected.segments[-1].content
    assert isinstance(outcome, TurnOutcome)
    assert outcome.interrupted_by_command_id == "c-stop"


def test_command_failure_and_noop_are_terminal_non_effects() -> None:
    projected = project(
        [
            admitted(1, INPUT),
            admitted(2, MODEL),
            entry(
                3, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="c-input", reason="harness gone"))
            ),
            entry(
                4,
                event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="c-model", reason="already that model")),
            ),
        ]
    )
    assert [summary.outcome for summary in projected.commands] == [
        Failed(origin_cursor=3, reason="harness gone"),
        Noop(origin_cursor=4, reason="already that model"),
    ]


def test_an_outcome_without_its_admission_is_dropped_rather_than_fabricated() -> None:
    """A replica projecting from a later checkpoint sees the effect but not the exact Command."""
    projected = project(
        [entry(9, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="c-gone", reason="late")))]
    )
    assert projected.commands == ()
    # The receipt boundary still exists, so grouping does not silently lose a position.
    assert contents(projected) == [CommandReceipt(command_id="c-gone")]


def test_harness_lifecycle_drives_the_controls(script: list[event_log_pb2.EventEntry]) -> None:
    running = project(script[:1])
    assert running.controls.harness_state == HarnessState.RUNNING
    assert contents(running) == [Lifecycle(state=HarnessState.RUNNING, exit_code=0, stopped_by_command_id="")]

    lost = project([entry(1, event_pb2.Event(harness_lost=event_pb2.HarnessLost()))])
    assert lost.controls.harness_state == HarnessState.LOST


def test_a_command_caused_exit_settles_its_command() -> None:
    stop = command_pb2.Command(command_id="c-halt", stop_runner_session=command_pb2.StopRunnerSession())
    projected = project(
        [
            admitted(1, stop),
            entry(
                2, event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=0, stopped_by_command_id="c-halt"))
            ),
        ]
    )
    (summary,) = projected.commands
    assert summary.operation_kind == OperationKind.STOP_RUNNER_SESSION
    assert summary.outcome == Effected(2)


def test_model_effects_keep_their_own_segments(script: list[event_log_pb2.EventEntry]) -> None:
    """Current model state cannot stand in for the historical changes that produced it."""
    projected = project(script)
    effects = [segment.content for segment in projected.segments if isinstance(segment.content, ModelEffect)]
    assert effects == [ModelEffect(command_id="c-model", previous_model="first-model", model="next-model")]


def test_an_uninterpreted_observation_halts_the_fold() -> None:
    with pytest.raises(UninterpretedObservationError) as raised:
        project(
            [
                entry(1, event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="x"))),
                event_log_pb2.EventEntry(cursor=2),
            ]
        )
    assert raised.value.cursor == 2


def test_entries_must_continue_the_projection(script: list[event_log_pb2.EventEntry]) -> None:
    projected = project(script[:5])
    with pytest.raises(ValueError, match="not after the projected"):
        advance(projected, [entry(3, event_pb2.Event(native=event_pb2.Native(line="{}")))])


def test_a_batch_must_continue_the_client(script: list[event_log_pb2.EventEntry]) -> None:
    _, batch = advance(project(script[:5]), script[5:10])
    with pytest.raises(ValueError, match="does not continue"):
        apply_batch(EMPTY, batch)


if __name__ == "__main__":
    pytest_bazel.main()
