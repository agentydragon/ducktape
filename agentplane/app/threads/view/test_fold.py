"""Behavioral coverage for the bounded thread fold."""

from dataclasses import dataclass, field, replace

import pytest
import pytest_bazel
from google.protobuf import json_format
from more_itertools import partitions

from agentplane.app.threads.view.fold import (
    AppendPayload,
    CommandOutcome,
    CommandSummary,
    EventBatch,
    EvidenceAssociation,
    FoldContractError,
    Item,
    ObservationNotUnderstoodError,
    PayloadField,
    PayloadRef,
    PriorEntities,
    ProjectionBatch,
    ReplacePayload,
    TextCompletion,
    ToolCompletion,
    ViewState,
    advance,
    initial,
    touched_keys,
)
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf

SOURCE = "test-source"
EPOCH = "test-epoch"


def entry(
    cursor: int, event: event_pb2.Event, *, source_sequences: list[int] | None = None
) -> event_log_pb2.EventEntry:
    if source_sequences is not None:
        event.source_sequences.extend(source_sequences)
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=SOURCE, sequence=cursor), event=event
    )


def admitted(
    cursor: int,
    command_id: str,
    operation: command_pb2.SubmitInput | command_pb2.ChangeModel | command_pb2.ChangeReasoningEffort,
) -> event_log_pb2.EventEntry:
    command = command_pb2.Command(command_id=command_id)
    if isinstance(operation, command_pb2.SubmitInput):
        command.submit_input.CopyFrom(operation)
    elif isinstance(operation, command_pb2.ChangeModel):
        command.change_model.CopyFrom(operation)
    else:
        command.change_reasoning_effort.CopyFrom(operation)
    return entry(cursor, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))


@dataclass
class Store:
    state: ViewState = field(default_factory=lambda: initial(SOURCE, EPOCH))
    items: dict[str, Item] = field(default_factory=dict)
    commands: dict[str, CommandSummary] = field(default_factory=dict)
    payloads: dict[PayloadRef, str] = field(default_factory=dict)
    evidence: list[EvidenceAssociation] = field(default_factory=list)

    def apply(self, entries: list[event_log_pb2.EventEntry]) -> ProjectionBatch:
        batch = EventBatch(SOURCE, self.state.position.through_cursor, tuple(entries))
        keys = touched_keys(batch)
        items = {item_id: self.items.get(item_id) for item_id in keys.item_ids}
        result = advance(
            self.state,
            batch,
            PriorEntities(
                items=items,
                commands={command_id: self.commands.get(command_id) for command_id in keys.command_ids},
                bodies={
                    reference: self.payloads[reference]
                    for item_id, payload_field in keys.completed
                    if (item := items[item_id]) is not None and (reference := item.payload(payload_field)) is not None
                },
            ),
        )
        for write in result.payload_writes:
            text = write.text
            if isinstance(write, AppendPayload) and write.base is not None:
                text = self.payloads[write.base] + text
            self.payloads[write.reference] = text
        self.items.update({item.item_id: item for item in result.item_upserts})
        self.commands.update({command.command_id: command for command in result.command_upserts})
        self.evidence.extend(result.evidence_upserts)
        self.state = result.state
        return result


@pytest.fixture
def script() -> list[event_log_pb2.EventEntry]:
    return [
        admitted(1, "input-1", command_pb2.SubmitInput(text="first")),
        admitted(2, "input-2", command_pb2.SubmitInput(text="second")),
        entry(
            3,
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id="message-1",
                    text="first\nsecond",
                    origin_command_ids=["input-1", "input-2"],
                    turn_id="turn-1",
                )
            ),
            source_sequences=[11, 12],
        ),
        entry(4, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="model-a"))),
        entry(5, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="answer", text="hel")), source_sequences=[13]),
        entry(
            6,
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(
                    item_id="tool-a", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="read"
                )
            ),
            source_sequences=[14],
        ),
        entry(
            7,
            event_pb2.Event(tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool-a", partial_json='{"p"')),
            source_sequences=[15],
        ),
        entry(
            8,
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(
                    item_id="tool-b", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="write"
                )
            ),
            source_sequences=[16],
        ),
        entry(
            9,
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool-b", tool=event_pb2.ToolResult(output="B", succeeded=True)
                )
            ),
            source_sequences=[17],
        ),
        entry(
            10,
            event_pb2.Event(tool_arguments=event_pb2.ToolArguments(item_id="tool-a", arguments_json='{"path":"x"}')),
            source_sequences=[18],
        ),
        entry(
            11,
            event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool-a", text="part")),
            source_sequences=[19],
        ),
        entry(
            12,
            event_pb2.Event(
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool-a", tool=event_pb2.ToolResult(output="", succeeded=False)
                )
            ),
            source_sequences=[20],
        ),
        entry(
            13,
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id="answer", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            ),
            source_sequences=[21],
        ),
        entry(
            14,
            event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="answer", text="hello")),
            source_sequences=[22],
        ),
        admitted(15, "model-1", command_pb2.ChangeModel(model="model-b")),
        entry(16, event_pb2.Event(model_changed=event_pb2.ModelChanged(command_id="model-1", model="model-b"))),
        entry(
            17,
            event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id="turn-1", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        ),
    ]


def replay(entries: list[event_log_pb2.EventEntry], sizes: list[int]) -> Store:
    store = Store()
    start = 0
    for size in sizes:
        store.apply(entries[start : start + size])
        start += size
    assert start == len(entries)
    return store


def test_effort_change_settles_its_command_and_updates_controls() -> None:
    store = Store()
    result = store.apply(
        [
            admitted(1, "effort-1", command_pb2.ChangeReasoningEffort(effort="high")),
            entry(
                2,
                event_pb2.Event(
                    reasoning_effort_changed=event_pb2.ReasoningEffortChanged(
                        command_id="effort-1", previous_effort="low", effort="high"
                    )
                ),
            ),
        ]
    )
    assert store.state.controls.applied_reasoning_effort == "high"
    assert store.commands["effort-1"].outcome == CommandOutcome.EFFECTED
    assert result.lifecycle_upserts[0].observation == "reasoning_effort_changed"


def test_batch_partitions_have_identical_materialization(script: list[event_log_pb2.EventEntry]) -> None:
    entries = script
    whole = replay(entries, [len(entries)])
    for split in range(1, len(entries)):
        assert replay(entries, [split, len(entries) - split]) == whole


def test_parallel_old_item_updates_keep_positions_fields_and_evidence(script: list[event_log_pb2.EventEntry]) -> None:
    store = replay(script, [17])
    answer, first, second = store.items["answer"], store.items["tool-a"], store.items["tool-b"]
    assert (answer.cursor, first.cursor, second.cursor) == (5, 6, 8)
    assert (first.revision_cursor, second.revision_cursor) == (12, 9)
    assert answer.turn_id == first.turn_id == second.turn_id == "turn-1"
    assert first.arguments is not None
    assert first.output is not None
    assert store.payloads[first.arguments] == '{"path":"x"}'
    assert store.payloads[first.output] == ""
    assert first.output.field is PayloadField.OUTPUT
    assert first.arguments.field is PayloadField.ARGUMENTS
    assert first.completion == ToolCompletion(succeeded=False)
    assert answer.completion == TextCompletion()
    assert {e.observation_cursor for e in store.evidence if e.entity_cursor == first.cursor} == {6, 7, 10, 11, 12}


def streaming(cursor: int, payload_field: PayloadField, text: str) -> event_log_pb2.EventEntry:
    match payload_field:
        case PayloadField.TEXT:
            return entry(cursor, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item", text=text)))
        case PayloadField.ARGUMENTS:
            delta = event_pb2.ToolArgumentsDelta(item_id="item", partial_json=text)
            return entry(cursor, event_pb2.Event(tool_arguments_delta=delta))
        case PayloadField.OUTPUT:
            return entry(
                cursor, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="item", text=text))
            )
    raise ValueError(payload_field)


def authoritative(cursor: int, payload_field: PayloadField, text: str) -> event_log_pb2.EventEntry:
    match payload_field:
        case PayloadField.TEXT:
            return entry(cursor, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="item", text=text)))
        case PayloadField.ARGUMENTS:
            arguments = event_pb2.ToolArguments(item_id="item", arguments_json=text)
            return entry(cursor, event_pb2.Event(tool_arguments=arguments))
        case PayloadField.OUTPUT:
            result = event_pb2.ToolResult(output=text, succeeded=True)
            return entry(cursor, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="item", tool=result)))
    raise ValueError(payload_field)


@pytest.mark.parametrize("payload_field", [PayloadField.TEXT, PayloadField.ARGUMENTS, PayloadField.OUTPUT])
@pytest.mark.parametrize(
    ("deltas", "final", "kept"),
    [(["hel", "lo"], "hello", True), (["hel", "lo"], "Hello", False), ([], "hello", False)],
    ids=["as-streamed", "rewritten", "not-streamed"],
)
def test_an_authoritative_value_keeps_the_body_only_when_it_is_what_streamed(
    payload_field: PayloadField, deltas: list[str], final: str, kept: bool
) -> None:
    entries = [
        *(streaming(cursor, payload_field, text) for cursor, text in enumerate(deltas, 1)),
        authoritative(len(deltas) + 1, payload_field, final),
    ]
    cursor = len(entries)
    reference = PayloadRef(EPOCH, 1, "item", payload_field, cursor, 1 if kept else cursor)
    # Kept: the streamed revision's generation and chunks, and no new text to store.
    last_write = (
        AppendPayload(replace(reference, revision_cursor=cursor - 1), reference, "")
        if kept
        else ReplacePayload(reference, final)
    )
    whole = Store()
    whole.apply(entries)
    assert whole.items["item"].payload(payload_field) == reference
    assert whole.payloads[reference] == final
    for batches in partitions(entries):
        store = Store()
        results = [store.apply(batch) for batch in batches]
        assert store == whole
        assert results[-1].payload_writes[-1] == last_write


def test_commands_settle_coalesced_input_and_observed_model_effect(script: list[event_log_pb2.EventEntry]) -> None:
    store = replay(script, [17])
    assert store.commands["input-1"].outcome is CommandOutcome.EFFECTED
    assert store.commands["input-2"].outcome is CommandOutcome.EFFECTED
    assert store.commands["model-1"].outcome is CommandOutcome.EFFECTED
    assert store.state.controls.applied_model == "model-b"
    assert store.state.controls.active_turn_id is None
    assert [(e.entity_cursor, e.observation_cursor) for e in store.evidence if e.entity_cursor in (1, 2, 15)] == [
        (1, 1),
        (2, 2),
        (1, 3),
        (2, 3),
        (15, 15),
        (15, 16),
    ]


def test_missing_lookup_is_not_absence_and_preloaded_rows_cannot_be_from_this_batch() -> None:
    observed = entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item", text="x")))
    with pytest.raises(FoldContractError):
        advance(initial(SOURCE, EPOCH), EventBatch(SOURCE, 0, (observed,)), PriorEntities({}, {}, {}))
    future = Item(EPOCH, "item", 1, 2)
    with pytest.raises(FoldContractError):
        advance(initial(SOURCE, EPOCH), EventBatch(SOURCE, 0, (observed,)), PriorEntities({"item": future}, {}, {}))
    store = Store()
    store.apply([observed])
    completion = authoritative(2, PayloadField.TEXT, "x")
    with pytest.raises(FoldContractError):
        advance(store.state, EventBatch(SOURCE, 1, (completion,)), PriorEntities({"item": store.items["item"]}, {}, {}))


def test_rejects_wrong_field_ref_unknown_kind_and_does_not_mutate_inputs_on_failure() -> None:
    store = Store()
    store.apply([entry(1, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item", text="x")))])
    prior = store.items["item"]
    invalid = Item(
        EPOCH, "item", prior.cursor, prior.revision_cursor, text=PayloadRef(EPOCH, 1, "item", PayloadField.OUTPUT, 1, 1)
    )
    next_entry = entry(2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item", text="y")))
    with pytest.raises(FoldContractError):
        advance(store.state, EventBatch(SOURCE, 1, (next_entry,)), PriorEntities({"item": invalid}, {}, {}))
    unknown = entry(2, json_format.ParseDict({"itemStarted": {"itemId": "other", "kind": 99}}, event_pb2.Event()))
    before = unknown.SerializeToString(), store.state
    with pytest.raises(ObservationNotUnderstoodError):
        advance(store.state, EventBatch(SOURCE, 1, (unknown,)), PriorEntities({"other": None}, {}, {}))
    assert unknown.SerializeToString() == before[0]
    assert store.state == before[1]


@pytest.mark.parametrize(
    "disposition",
    [
        event_pb2.RECOVERY_DISPOSITION_RETAINED,
        event_pb2.RECOVERY_DISPOSITION_ABSENT,
        event_pb2.RECOVERY_DISPOSITION_UNKNOWN,
    ],
)
def test_reconciliation_preserves_observed_tool_success_and_payload(disposition: event_pb2.RecoveryDisposition) -> None:
    store = Store()
    store.apply(
        [
            entry(
                1,
                event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="tool", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
            ),
            entry(
                2,
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(
                        item_id="tool", tool=event_pb2.ToolResult(output="side effect committed", succeeded=True)
                    )
                ),
            ),
        ]
    )
    original = store.items["tool"]
    result = store.apply(
        [
            entry(
                3,
                event_pb2.Event(
                    conversation_reconciled=event_pb2.ConversationReconciled(
                        turn_id="interrupted", items=[event_pb2.ItemRecovery(item_id="tool", disposition=disposition)]
                    )
                ),
            )
        ]
    )
    recovered = store.items["tool"]
    assert recovered.completion == ToolCompletion(succeeded=True)
    assert recovered.output == original.output
    assert recovered.recovery == disposition
    assert result.payload_writes == ()


def test_recovered_tool_content_does_not_fabricate_an_execution_result() -> None:
    store = Store()
    store.apply(
        [
            entry(
                1,
                event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="tool", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
            ),
            entry(
                2, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool", text="partial stdout"))
            ),
        ]
    )
    original = store.items["tool"].output
    store.apply(
        [
            entry(
                3,
                event_pb2.Event(
                    conversation_reconciled=event_pb2.ConversationReconciled(
                        turn_id="lost",
                        items=[
                            event_pb2.ItemRecovery(
                                item_id="tool",
                                disposition=event_pb2.RECOVERY_DISPOSITION_REVISED,
                                replacement=event_pb2.RecoveredContent(
                                    arguments_json='{"command":"run"}', output="interrupted"
                                ),
                            )
                        ],
                    )
                ),
            )
        ]
    )
    recovered = store.items["tool"]
    assert recovered.completion is None
    assert recovered.output is not None
    assert store.payloads[recovered.output] == "interrupted"
    assert original is not None
    assert store.payloads[original] == "partial stdout"
    assert recovered.recovery == event_pb2.RECOVERY_DISPOSITION_REVISED


def _recovery(disposition: event_pb2.RecoveryDisposition, reason: str) -> event_pb2.ItemRecovery:
    return event_pb2.ItemRecovery(
        item_id="tool",
        disposition=disposition,
        reason=reason,
        replacement=(
            event_pb2.RecoveredContent(arguments_json='{"command":"run"}', output="interrupted")
            if disposition == event_pb2.RECOVERY_DISPOSITION_REVISED
            else None
        ),
    )


@pytest.mark.parametrize(
    ("first", "second", "second_wins"),
    [
        (event_pb2.RECOVERY_DISPOSITION_RETAINED, event_pb2.RECOVERY_DISPOSITION_UNKNOWN, False),
        (event_pb2.RECOVERY_DISPOSITION_ABSENT, event_pb2.RECOVERY_DISPOSITION_UNKNOWN, False),
        (event_pb2.RECOVERY_DISPOSITION_REVISED, event_pb2.RECOVERY_DISPOSITION_UNKNOWN, False),
        (event_pb2.RECOVERY_DISPOSITION_UNKNOWN, event_pb2.RECOVERY_DISPOSITION_RETAINED, True),
        (event_pb2.RECOVERY_DISPOSITION_UNKNOWN, event_pb2.RECOVERY_DISPOSITION_UNKNOWN, True),
        (event_pb2.RECOVERY_DISPOSITION_RETAINED, event_pb2.RECOVERY_DISPOSITION_ABSENT, True),
        (event_pb2.RECOVERY_DISPOSITION_ABSENT, event_pb2.RECOVERY_DISPOSITION_REVISED, True),
    ],
)
def test_a_later_reconciliation_replaces_a_decision_unless_it_could_not_tell(
    first: event_pb2.RecoveryDisposition, second: event_pb2.RecoveryDisposition, second_wins: bool
) -> None:
    store = Store()
    store.apply(
        [
            entry(
                1,
                event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="tool", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
            )
        ]
    )
    for cursor, (disposition, reason) in enumerate([(first, "first"), (second, "second")], start=2):
        store.apply(
            [
                entry(
                    cursor,
                    event_pb2.Event(
                        conversation_reconciled=event_pb2.ConversationReconciled(
                            turn_id=f"turn-{cursor}", items=[_recovery(disposition, reason)]
                        )
                    ),
                )
            ]
        )
    shown = store.items["tool"]
    assert (shown.recovery, shown.recovery_reason) == ((second, "second") if second_wins else (first, "first"))


def test_setup_output_and_failure_remain_visible_lifecycle_evidence() -> None:
    store = Store()
    result = store.apply(
        [
            entry(1, event_pb2.Event(setup_started=event_pb2.SetupStarted())),
            entry(2, event_pb2.Event(setup_output=event_pb2.SetupOutput(stdout=b"setup output\n"))),
            entry(3, event_pb2.Event(setup_finished=event_pb2.SetupFinished(exit_code=7))),
        ]
    )
    assert [segment.observation for segment in result.lifecycle_upserts] == [
        "setup_started",
        "setup_output",
        "setup_finished",
    ]
    assert result.lifecycle_upserts[1].event.setup_output.stdout == b"setup output\n"
    assert store.state.position.through_cursor == 3


if __name__ == "__main__":
    pytest_bazel.main()
