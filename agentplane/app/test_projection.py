"""Server projection semantics and parity across persistence batch boundaries."""

import random
from dataclasses import dataclass, field

import pytest
import pytest_bazel

from agentplane.app import thread_view_pb2
from agentplane.app.projection import (
    AppendPayload,
    EventBatch,
    PriorEntities,
    ProjectionBatch,
    ReplacePayload,
    UninterpretedObservationError,
    advance,
    empty,
)
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf

SOURCE = "source-under-test"
EPOCH = "epoch-under-test"


def entry(cursor: int, event: event_pb2.Event) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=SOURCE, sequence=cursor), event=event
    )


def admitted(cursor: int, command: command_pb2.Command) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))


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


def payload_key(reference: thread_view_pb2.PayloadRef) -> bytes:
    return reference.SerializeToString(deterministic=True)


@dataclass
class Store:
    state: thread_view_pb2.ViewState = field(default_factory=lambda: empty(SOURCE, EPOCH))
    segments: dict[int, thread_view_pb2.Segment] = field(default_factory=dict)
    items: dict[str, thread_view_pb2.Segment] = field(default_factory=dict)
    commands: dict[str, thread_view_pb2.CommandSummary] = field(default_factory=dict)
    payloads: dict[bytes, str] = field(default_factory=dict)

    def apply(self, entries: list[event_log_pb2.EventEntry]) -> ProjectionBatch:
        # The test store supplies indexed absences for observations in this finite fixture.
        item_ids = {"i-1", "i-2", "i-3", "a", "b"}
        command_ids = {"c-input", "c-model", "c-stop", "second", "halt"}
        prior = PriorEntities(
            items={key: self.items.get(key) for key in item_ids},
            commands={key: self.commands.get(key) for key in command_ids},
        )
        result = advance(self.state, EventBatch(SOURCE, self.state.position.through_cursor, tuple(entries)), prior)
        for operation in result.payload_writes:
            text = operation.text
            if isinstance(operation, AppendPayload) and operation.base is not None:
                text = self.payloads[payload_key(operation.base)] + text
            self.payloads[payload_key(operation.reference)] = text
        for segment in result.segment_upserts:
            self.segments[segment.cursor] = segment
            if segment.WhichOneof("content") == "item":
                self.items[segment.item.item_id] = segment
        self.commands.update({summary.command_id: summary for summary in result.command_upserts})
        self.state = result.state
        return result

    def text(self, content: thread_view_pb2.Text) -> str:
        assert not content.HasField("value")
        return self.payloads[payload_key(content.reference)]


def replay(entries: list[event_log_pb2.EventEntry], boundaries: list[int]) -> Store:
    store, start = Store(), 0
    for size in boundaries:
        store.apply(entries[start : start + size])
        start += size
    assert start == len(entries)
    return store


def test_batch_boundaries_do_not_change_materialization(script: list[event_log_pb2.EventEntry]) -> None:
    whole = replay(script, [len(script)])
    for split in range(len(script) + 1):
        assert replay(script, [split, len(script) - split]) == whole
    generator = random.Random(20260920)
    for _ in range(100):
        boundaries, remaining = [], len(script)
        while remaining:
            size = generator.randint(1, remaining)
            boundaries.append(size)
            remaining -= size
        assert replay(script, boundaries) == whole


def test_item_content_and_command_semantics(script: list[event_log_pb2.EventEntry]) -> None:
    store = replay(script, [len(script)])
    reasoning, tool = store.items["i-1"].item, store.items["i-2"].item
    assert store.text(reasoning.text) == "thinking"
    assert reasoning.WhichOneof("completion") == "completed_text"
    assert store.text(tool.arguments_json) == '{"path": "a"}'
    assert store.text(tool.output) == "whole output"
    assert tool.completed_tool.succeeded
    assert tool.turn_id == "turn-1"
    assert store.text(store.commands["c-input"].input) == "inspect this"
    assert store.commands["c-input"].effected.origin_cursor == 3
    assert store.commands["c-model"].effected.origin_cursor == 17
    assert store.state.unresolved_count == 0
    assert store.state.controls.applied_model == "next-model"
    assert not store.state.controls.HasField("active_turn_id")
    assert store.state.controls.harness_state == thread_view_pb2.HARNESS_STATE_STOPPED
    assert store.segments[4].event.turn_started.turn_id == "turn-1"
    assert store.segments[21].event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED


def test_only_touched_entities_are_upserted(script: list[event_log_pb2.EventEntry]) -> None:
    store = replay(script[:10], [10])
    update = store.apply(script[10:14])
    assert [segment.cursor for segment in update.segment_upserts] == [10]
    assert update.segment_upserts[0].revision_cursor == 14
    assert not update.command_upserts
    assert update.state.position.through_cursor == 14


def test_debug_events_only_advance_checkpoint() -> None:
    result = Store().apply(
        [
            entry(1, event_pb2.Event(native=event_pb2.Native(line="{}"))),
            entry(2, event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="warning"))),
            entry(3, event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="test"))),
        ]
    )
    assert result.state.position.through_cursor == 3
    assert not result.segment_upserts
    assert not result.command_upserts
    assert not result.payload_writes


def test_append_operations_do_not_require_historical_bodies() -> None:
    store = Store()
    first = store.apply([entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="early")))])
    second = store.apply([entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text=" text")))])
    assert first.payload_writes == (
        AppendPayload(
            None,
            thread_view_pb2.PayloadRef(
                source_id=SOURCE,
                projection_epoch=EPOCH,
                owner_cursor=1,
                field=thread_view_pb2.PAYLOAD_FIELD_TEXT,
                revision_cursor=1,
            ),
            "early",
        ),
    )
    assert isinstance(second.payload_writes[0], AppendPayload)
    assert second.payload_writes[0].base == first.payload_writes[0].reference
    assert second.payload_writes[0].text == " text"
    completed = store.apply(
        [entry(3, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="a", text="different")))]
    )
    assert isinstance(completed.payload_writes[0], ReplacePayload)
    assert completed.payload_writes[0].text == "different"
    assert store.text(store.items["a"].item.text) == "different"


def test_independent_field_references_and_out_of_order_tools() -> None:
    store = Store()
    store.apply(
        [
            entry(1, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="old-turn"))),
            entry(
                2, event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="a", kind=event_pb2.ITEM_KIND_TOOL_CALL))
            ),
            entry(3, event_pb2.Event(tool_arguments=event_pb2.ToolArguments(item_id="a", arguments_json="{}"))),
            entry(
                4, event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="b", kind=event_pb2.ITEM_KIND_TOOL_CALL))
            ),
        ]
    )
    arguments = store.items["a"].item.arguments_json.reference
    store.apply(
        [
            entry(
                5,
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(
                        item_id="b", tool=event_pb2.ToolResult(output="B", succeeded=True)
                    )
                ),
            ),
            entry(6, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="new-turn"))),
            entry(7, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="a", text="part"))),
            entry(
                8,
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(
                        item_id="a", tool=event_pb2.ToolResult(output="A", succeeded=False)
                    )
                ),
            ),
        ]
    )
    a, b = store.items["a"], store.items["b"]
    assert (a.cursor, b.cursor) == (2, 4)
    assert (a.revision_cursor, b.revision_cursor) == (8, 5)
    assert a.item.turn_id == b.item.turn_id == "old-turn"
    assert a.item.arguments_json.reference == arguments
    assert store.text(a.item.output) == "A"
    assert not a.item.completed_tool.succeeded
    assert b.item.completed_tool.succeeded


def test_first_delta_owns_position_even_if_start_arrives_later() -> None:
    store = Store()
    store.apply(
        [
            entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="early"))),
            entry(
                2,
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id="a", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                ),
            ),
        ]
    )
    assert (store.items["a"].cursor, store.items["a"].revision_cursor) == (1, 2)
    assert store.items["a"].item.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT
    assert store.text(store.items["a"].item.text) == "early"


def test_process_loss_does_not_complete_items() -> None:
    store = Store()
    store.apply(
        [
            entry(1, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-turn"))),
            entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="unfinished"))),
            entry(3, event_pb2.Event(harness_lost=event_pb2.HarnessLost())),
        ]
    )
    assert store.items["a"].item.WhichOneof("completion") is None
    assert store.state.controls.harness_state == thread_view_pb2.HARNESS_STATE_LOST
    assert not store.state.controls.HasField("active_turn_id")


def test_model_is_applied_only_at_observed_effect(script: list[event_log_pb2.EventEntry]) -> None:
    store = replay(script[:16], [16])
    assert store.commands["c-model"].WhichOneof("outcome") == "pending"
    assert store.state.unresolved_count == 1
    assert store.state.controls.applied_model == "first-model"
    store.apply(script[16:17])
    assert store.state.unresolved_count == 0
    assert store.state.controls.applied_model == "next-model"


def test_coalesced_input_settles_all_commands_and_keeps_provenance() -> None:
    store = Store()
    confirmed = event_pb2.HarnessUserMessageConfirmed(text="both", origin_command_ids=["c-input", "second"])
    store.apply(
        [
            admitted(1, INPUT),
            admitted(2, command_pb2.Command(command_id="second", submit_input=command_pb2.SubmitInput(text="second"))),
            entry(3, event_pb2.Event(harness_user_message_confirmed=confirmed, source_sequences=[11, 12])),
        ]
    )
    assert store.state.unresolved_count == 0
    assert [summary.effected.origin_cursor for summary in store.commands.values()] == [3, 3]
    materialized = store.segments[3].confirmed_input
    assert list(materialized.origin_command_ids) == ["c-input", "second"]
    assert store.text(materialized.text) == "both"
    assert materialized.text.reference.field == thread_view_pb2.PAYLOAD_FIELD_CONFIRMED_INPUT


def test_non_effect_outcomes_and_interrupt() -> None:
    store = Store()
    store.apply(
        [
            admitted(1, INPUT),
            admitted(2, MODEL),
            admitted(3, INTERRUPT),
            entry(4, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="c-input", reason="gone"))),
            entry(5, event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="c-model", reason="same model"))),
            entry(
                6,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="turn-1", status=event_pb2.TURN_STATUS_INTERRUPTED, interrupted_by_command_id="c-stop"
                    )
                ),
            ),
        ]
    )
    assert store.commands["c-input"].failed.reason == "gone"
    assert store.commands["c-model"].noop.reason == "same model"
    assert store.commands["c-stop"].effected.origin_cursor == 6
    assert store.state.unresolved_count == 0


@pytest.mark.parametrize(
    ("source", "after", "cursor", "match"),
    [
        ("wrong-source", 0, 1, "source"),
        (SOURCE, 1, 2, "checkpoint"),
        (SOURCE, 0, 2, "noncontiguous"),
        (SOURCE, 0, 0, "noncontiguous"),
    ],
)
def test_invalid_source_or_checkpoint(source: str, after: int, cursor: int, match: str) -> None:
    state = empty(SOURCE, EPOCH)
    with pytest.raises(ValueError, match=match):
        advance(
            state,
            EventBatch(source, after, (entry(cursor, event_pb2.Event(native=event_pb2.Native())),)),
            PriorEntities({}, {}),
        )
    assert state == empty(SOURCE, EPOCH)


def test_missing_prior_lookup_is_not_treated_as_a_new_item() -> None:
    with pytest.raises(ValueError, match="missing prior item lookup"):
        advance(
            empty(SOURCE, EPOCH),
            EventBatch(SOURCE, 0, (entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="x"))),)),
            PriorEntities({}, {}),
        )


def test_missing_command_admission_is_an_error() -> None:
    with pytest.raises(ValueError, match="missing admitted command"):
        Store().apply(
            [entry(1, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="c-input", reason="late")))]
        )


def test_failed_batch_does_not_modify_supplied_entities() -> None:
    store = Store()
    store.apply(
        [admitted(1, INPUT), entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="initial")))]
    )
    state_before = store.state.SerializeToString()
    item_before = store.items["a"].SerializeToString()
    command_before = store.commands["c-input"].SerializeToString()
    with pytest.raises(UninterpretedObservationError):
        store.apply(
            [
                entry(3, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text=" more"))),
                entry(4, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="c-input", reason="gone"))),
                entry(5, event_pb2.Event()),
            ]
        )
    assert store.state.SerializeToString() == state_before
    assert store.items["a"].SerializeToString() == item_before
    assert store.commands["c-input"].SerializeToString() == command_before


def test_unresolved_count_cannot_underflow_or_double_settle() -> None:
    store = Store()
    store.apply([admitted(1, INPUT)])
    outcome = entry(2, event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="c-input", reason="test")))
    store.state.unresolved_count = 0
    with pytest.raises(ValueError, match="underflow"):
        store.apply([outcome])
    store.state.unresolved_count = 1
    store.apply([outcome])
    outcome.cursor = 3
    outcome.origin.sequence = 3
    with pytest.raises(ValueError, match="already settled"):
        store.apply([outcome])


@pytest.mark.parametrize(("source", "sequence"), [("other-source", 1), (SOURCE, 2), ("", 0)])
def test_entry_origin_must_match_original_archive(source: str, sequence: int) -> None:
    observed = entry(1, event_pb2.Event(native=event_pb2.Native()))
    observed.origin.source_id = source
    observed.origin.sequence = sequence
    with pytest.raises(ValueError, match="original source archive"):
        Store().apply([observed])


def test_wrong_generation_payload_reference_is_refused() -> None:
    store = Store()
    store.apply([entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="old")))])
    store.items["a"].item.text.reference.projection_epoch = "different-epoch"
    with pytest.raises(ValueError, match="projected prefix"):
        store.apply([entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="next")))])


def test_stopped_by_command_and_failed_turn_remain_visible() -> None:
    store = Store()
    stop = command_pb2.Command(command_id="halt", stop_runner_session=command_pb2.StopRunnerSession())
    store.apply(
        [
            entry(1, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-turn"))),
            entry(
                2,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="test-turn", status=event_pb2.TURN_STATUS_FAILED, error="upstream failure"
                    )
                ),
            ),
            admitted(3, stop),
            entry(4, event_pb2.Event(harness_exited=event_pb2.HarnessExited(stopped_by_command_id="halt"))),
        ]
    )
    assert store.segments[2].event.turn_completed.error == "upstream failure"
    assert store.commands["halt"].effected.origin_cursor == 4
    assert store.state.unresolved_count == 0
    assert store.state.controls.harness_state == thread_view_pb2.HARNESS_STATE_STOPPED


def test_interleaved_old_items_keep_independent_revisions() -> None:
    store = Store()
    store.apply(
        [
            entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="A"))),
            entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="b", text="B"))),
        ]
    )
    a_text = store.items["a"].item.text.reference
    update = store.apply(
        [
            entry(
                3,
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id="a", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                ),
            ),
            entry(4, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="b", text="2"))),
            entry(
                5,
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id="a", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                ),
            ),
            entry(6, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="b", text="3"))),
        ]
    )
    assert {segment.cursor for segment in update.segment_upserts} == {1, 2}
    assert store.items["a"].revision_cursor == 5
    assert store.items["a"].item.text.reference == a_text
    assert store.items["b"].revision_cursor == 6
    assert store.text(store.items["b"].item.text) == "B23"
    assert len(update.payload_writes) == 2


def test_prior_lookup_cannot_come_from_later_in_this_batch() -> None:
    store = Store()
    store.apply([entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="old")))])
    store.items["a"].revision_cursor = 2
    with pytest.raises(ValueError, match="invalid prior item"):
        store.apply(
            [
                entry(2, event_pb2.Event(native=event_pb2.Native())),
                entry(3, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="a", text="new"))),
            ]
        )


if __name__ == "__main__":
    pytest_bazel.main()
