"""Golden vectors that pin the Go thread fold (`agentplane/history_service/threadfold`) to this one.

Each scenario is folded here, batch by batch, against rows kept the way a storage worker keeps them. A
batch records its input entries, the rows the fold materializes, its payload writes and its evidence, or
the error it raises. The Go fold must reproduce all of them from the same entries, reading the rows of
earlier batches as its prior state.

Regenerate with `bb run //agentplane/history_service/fold:generate_golden_bin -- <path to golden.json>`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import cast
from uuid import UUID

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.history_service.fold.fold import (
    AppendPayload,
    CommandSummary,
    EventBatch,
    EvidenceAssociation,
    FoldContractError,
    Item,
    ObservationNotUnderstoodError,
    PayloadRef,
    PayloadWrite,
    PriorEntities,
    ViewState,
    advance,
    initial,
    touched_keys,
)
from agentplane.history_service.fold.rows import ordered_entity_rows, payload_references
from agentplane.history_service.fold.views import PayloadField, ThreadFeedErrorState, ThreadOperationalState
from agentplane.proto import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf

SOURCE = "test-source"
EPOCH = "test-epoch"
THREAD_ID = UUID("00000000-0000-4000-8000-000000000001")

Json = dict[str, object]

# Enum numbers the schema does not name, which an open enum still carries.
UNKNOWN_ITEM_KIND = cast(event_pb2.ItemKind, 99)
UNKNOWN_TURN_STATUS = cast(event_pb2.TurnStatus, 99)
UNKNOWN_DISPOSITION = cast(event_pb2.RecoveryDisposition, 9)


def at(seconds: int, nanos: int = 0) -> Timestamp:
    return Timestamp(seconds=1_800_000_000 + seconds, nanos=nanos)


def entry(cursor: int, event: event_pb2.Event, *sequences: int, source_id: str = SOURCE) -> event_log_pb2.EventEntry:
    event.source_sequences.extend(sequences)
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=source_id, sequence=cursor), event=event
    )


def text_delta(cursor: int, item_id: str, text: str, *sequences: int) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item_id, text=text)), *sequences)


def arguments_delta(cursor: int, item_id: str, partial_json: str) -> event_log_pb2.EventEntry:
    delta = event_pb2.ToolArgumentsDelta(item_id=item_id, partial_json=partial_json)
    return entry(cursor, event_pb2.Event(tool_arguments_delta=delta))


def output_delta(cursor: int, item_id: str, text: str) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id=item_id, text=text)))


def tool_arguments(cursor: int, item_id: str, arguments_json: str, *sequences: int) -> event_log_pb2.EventEntry:
    arguments = event_pb2.ToolArguments(item_id=item_id, arguments_json=arguments_json)
    return entry(cursor, event_pb2.Event(tool_arguments=arguments), *sequences)


def item_started(
    cursor: int, item_id: str, kind: event_pb2.ItemKind, tool_name: str = "", *sequences: int
) -> event_log_pb2.EventEntry:
    started = event_pb2.ItemStarted(item_id=item_id, kind=kind, tool_name=tool_name)
    return entry(cursor, event_pb2.Event(item_started=started), *sequences)


def text_completed(cursor: int, item_id: str, text: str, *sequences: int) -> event_log_pb2.EventEntry:
    completed = event_pb2.ItemCompleted(item_id=item_id, text=text)
    return entry(cursor, event_pb2.Event(item_completed=completed), *sequences)


def tool_completed(
    cursor: int, item_id: str, output: str, succeeded: bool, *sequences: int
) -> event_log_pb2.EventEntry:
    completed = event_pb2.ItemCompleted(item_id=item_id, tool=event_pb2.ToolResult(output=output, succeeded=succeeded))
    return entry(cursor, event_pb2.Event(item_completed=completed), *sequences)


def admitted(cursor: int, command: command_pb2.Command) -> event_log_pb2.EventEntry:
    return entry(cursor, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))


def submit_input(cursor: int, command_id: str, text: str) -> event_log_pb2.EventEntry:
    return admitted(cursor, command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text)))


def change_model(cursor: int, command_id: str, model: str) -> event_log_pb2.EventEntry:
    return admitted(
        cursor, command_pb2.Command(command_id=command_id, change_model=command_pb2.ChangeModel(model=model))
    )


def reconciled(cursor: int, turn_id: str, *items: event_pb2.ItemRecovery) -> event_log_pb2.EventEntry:
    return entry(
        cursor, event_pb2.Event(conversation_reconciled=event_pb2.ConversationReconciled(turn_id=turn_id, items=items))
    )


@dataclass(frozen=True)
class Batch:
    entries: tuple[event_log_pb2.EventEntry, ...]
    settled: tuple[tuple[int, int], ...] = ()
    operational: ThreadOperationalState | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    batches: tuple[Batch, ...]


def single(*entries: event_log_pb2.EventEntry) -> tuple[Batch, ...]:
    """One batch per entry, so every entry reads its predecessors back as stored rows."""
    return tuple(Batch((one,)) for one in entries)


def whole(*entries: event_log_pb2.EventEntry) -> tuple[Batch, ...]:
    return (Batch(entries),)


def ref_json(reference: PayloadRef) -> Json:
    return {
        "projection_epoch": reference.projection_epoch,
        "owner_cursor": reference.owner_cursor,
        "owner_id": reference.owner_id,
        "field": str(reference.field),
        "revision_cursor": reference.revision_cursor,
        "generation": reference.generation,
    }


def write_json(write: PayloadWrite) -> Json:
    if isinstance(write, AppendPayload):
        base = None if write.base is None else ref_json(write.base)
        return {"kind": "append", "base": base, "reference": ref_json(write.reference), "text": write.text}
    return {"kind": "replace", "reference": ref_json(write.reference), "text": write.text}


def evidence_json(evidence: EvidenceAssociation) -> Json:
    return {
        "projection_epoch": evidence.projection_epoch,
        "entity_cursor": evidence.entity_cursor,
        "observation_cursor": evidence.observation_cursor,
        "source_sequences": list(evidence.source_sequences),
    }


class Memory:
    """The rows a storage worker keeps: what the fold's callers preload for the next batch."""

    def __init__(self) -> None:
        self.state: ViewState = initial(SOURCE, EPOCH)
        self.items: dict[str, Item] = {}
        self.commands: dict[str, CommandSummary] = {}
        self.payloads: dict[PayloadRef, str] = {}

    def apply(self, batch: Batch) -> Json:
        event_batch = EventBatch(SOURCE, self.state.position.through_cursor, batch.entries, batch.settled)
        keys = touched_keys(event_batch)
        items = {item_id: self.items.get(item_id) for item_id in keys.item_ids}
        prior = PriorEntities(
            items=items,
            commands={command_id: self.commands.get(command_id) for command_id in keys.command_ids},
            bodies={
                reference: self.payloads[reference]
                for item_id, payload_field in keys.completed
                if (item := items[item_id]) is not None and (reference := item.payload(payload_field)) is not None
            },
        )
        recorded: Json = {
            "entries": [json_format.MessageToDict(one) for one in batch.entries],
            "settled": [list(cursor_range) for cursor_range in batch.settled],
        }
        if batch.operational is not None:
            recorded["operational"] = batch.operational.model_dump(mode="json")
        try:
            result = advance(self.state, event_batch, prior)
        except ObservationNotUnderstoodError as error:
            return {**recorded, "error": {"kind": "not_understood", "cursor": error.cursor}}
        except FoldContractError as error:
            return {**recorded, "error": {"kind": "contract", "cursor": error.cursor}}
        for write in result.payload_writes:
            text = write.text
            if isinstance(write, AppendPayload) and write.base is not None:
                text = self.payloads[write.base] + text
            self.payloads[write.reference] = text
        self.items.update({item.item_id: item for item in result.item_upserts})
        self.commands.update({command.command_id: command for command in result.command_upserts})
        self.state = result.state
        # The stored chunk counts are the storage worker's; any deterministic figure per reference checks
        # that each row reads its own.
        extents = {
            reference: 1 + (reference.owner_cursor + reference.revision_cursor + reference.generation) % 4
            for reference in payload_references(result)
        }
        return {
            **recorded,
            "extents": [
                {"ref": ref_json(reference), "count": extents[reference]}
                for reference in sorted(
                    extents,
                    key=lambda ref: (
                        ref.owner_cursor,
                        ref.owner_id,
                        str(ref.field),
                        ref.revision_cursor,
                        ref.generation,
                    ),
                )
            ],
            "expected": {
                "rows": ordered_entity_rows(THREAD_ID, result, extents, batch.operational),
                "payload_writes": [write_json(write) for write in result.payload_writes],
                "evidence": [evidence_json(evidence) for evidence in result.evidence_upserts],
            },
        }


def run(scenario: Scenario) -> Json:
    memory = Memory()
    batches: list[Json] = []
    for batch in scenario.batches:
        recorded = memory.apply(batch)
        batches.append(recorded)
        if "error" in recorded:
            break
    return {"name": scenario.name, "batches": batches}


def conversation() -> list[event_log_pb2.EventEntry]:
    return [
        submit_input(1, "input-1", "first"),
        submit_input(2, "input-2", "second"),
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
            11,
            12,
        ),
        entry(4, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="model-a"))),
        text_delta(5, "answer", "hel", 13),
        item_started(6, "tool-a", event_pb2.ITEM_KIND_TOOL_CALL, "read", 14),
        entry(
            7,
            event_pb2.Event(tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool-a", partial_json='{"p"')),
            15,
        ),
        item_started(8, "tool-b", event_pb2.ITEM_KIND_TOOL_CALL, "write", 16),
        tool_completed(9, "tool-b", "B", True, 17),
        tool_arguments(10, "tool-a", '{"path":"x"}', 18),
        entry(11, event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool-a", text="part")), 19),
        tool_completed(12, "tool-a", "", False, 20),
        item_started(13, "answer", event_pb2.ITEM_KIND_ASSISTANT_TEXT, "", 21),
        text_completed(14, "answer", "hello", 22),
        change_model(15, "model-1", "model-b"),
        entry(16, event_pb2.Event(model_changed=event_pb2.ModelChanged(command_id="model-1", model="model-b"))),
        entry(
            17,
            event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id="turn-1", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        ),
    ]


def streaming(payload_field: PayloadField, cursor: int, text: str) -> event_log_pb2.EventEntry:
    match payload_field:
        case PayloadField.TEXT:
            return text_delta(cursor, "item", text)
        case PayloadField.ARGUMENTS:
            return arguments_delta(cursor, "item", text)
        case PayloadField.OUTPUT:
            return output_delta(cursor, "item", text)
    raise ValueError(payload_field)


def authoritative(payload_field: PayloadField, cursor: int, text: str) -> event_log_pb2.EventEntry:
    match payload_field:
        case PayloadField.TEXT:
            return text_completed(cursor, "item", text)
        case PayloadField.ARGUMENTS:
            return tool_arguments(cursor, "item", text)
        case PayloadField.OUTPUT:
            return tool_completed(cursor, "item", text, True)
    raise ValueError(payload_field)


def authoritative_scenarios() -> list[Scenario]:
    scenarios = []
    for payload_field, (variant, deltas, final) in product(
        [PayloadField.TEXT, PayloadField.ARGUMENTS, PayloadField.OUTPUT],
        [("as streamed", ["hel", "lo"], "hello"), ("rewritten", ["hel", "lo"], "Hello"), ("not streamed", [], "hello")],
    ):
        entries = [streaming(payload_field, cursor, text) for cursor, text in enumerate(deltas, 1)]
        entries.append(authoritative(payload_field, len(entries) + 1, final))
        scenarios.append(Scenario(f"{payload_field} {variant}", single(*entries)))
    return scenarios


def recovery(
    disposition: event_pb2.RecoveryDisposition, reason: str = "", *, item_id: str = "tool"
) -> event_pb2.ItemRecovery:
    replacement = (
        event_pb2.RecoveredContent(arguments_json='{"command":"run"}', output="interrupted")
        if disposition == event_pb2.RECOVERY_DISPOSITION_REVISED
        else None
    )
    return event_pb2.ItemRecovery(item_id=item_id, disposition=disposition, reason=reason, replacement=replacement)


def reconciliation_scenarios() -> list[Scenario]:
    retained = event_pb2.RECOVERY_DISPOSITION_RETAINED
    absent = event_pb2.RECOVERY_DISPOSITION_ABSENT
    unknown = event_pb2.RECOVERY_DISPOSITION_UNKNOWN
    revised = event_pb2.RECOVERY_DISPOSITION_REVISED
    tool = item_started(1, "tool", event_pb2.ITEM_KIND_TOOL_CALL, "run")
    scenarios = [
        Scenario(
            f"reconcile a completed tool as {event_pb2.RecoveryDisposition.Name(disposition)}",
            single(
                tool,
                tool_completed(2, "tool", "side effect committed", True),
                reconciled(3, "interrupted", recovery(disposition, "why")),
            ),
        )
        for disposition in (retained, absent, unknown)
    ]
    scenarios += [
        Scenario(
            "reconcile a tool call with partial output as revised",
            single(
                tool, output_delta(2, "tool", "partial stdout"), reconciled(3, "lost", recovery(revised, "replaced"))
            ),
        ),
        Scenario(
            "reconcile an assistant text as revised",
            single(
                item_started(1, "answer", event_pb2.ITEM_KIND_ASSISTANT_TEXT),
                text_delta(2, "answer", "par"),
                reconciled(
                    3,
                    "lost",
                    event_pb2.ItemRecovery(
                        item_id="answer",
                        disposition=revised,
                        replacement=event_pb2.RecoveredContent(text="whole answer"),
                    ),
                ),
            ),
        ),
        Scenario(
            "reconcile several items in one event, one of them unseen",
            single(
                tool,
                reconciled(
                    2,
                    "lost",
                    recovery(absent, "gone"),
                    recovery(retained, item_id="unseen"),
                    recovery(unknown, item_id="unseen"),
                ),
            ),
        ),
    ]
    for first, second in [
        (retained, unknown),
        (absent, unknown),
        (revised, unknown),
        (unknown, retained),
        (unknown, unknown),
        (retained, absent),
        (absent, revised),
    ]:
        scenarios.append(
            Scenario(
                f"reconcile {event_pb2.RecoveryDisposition.Name(first)} then {event_pb2.RecoveryDisposition.Name(second)}",
                single(
                    tool,
                    reconciled(2, "first", recovery(first, "first")),
                    reconciled(3, "second", recovery(second, "second")),
                ),
            )
        )
    return scenarios


def lifecycle_scenarios() -> list[Scenario]:
    def timed(event: event_pb2.Event, seconds: int, nanos: int = 0) -> event_pb2.Event:
        event.at.CopyFrom(at(seconds, nanos))
        return event

    return [
        Scenario(
            "setup, launch and silent observations",
            whole(
                entry(1, timed(event_pb2.Event(setup_started=event_pb2.SetupStarted()), 0)),
                entry(
                    2,
                    timed(
                        event_pb2.Event(setup_output=event_pb2.SetupOutput(stdout=b"setup output\n")), 1, 120_000_000
                    ),
                ),
                entry(
                    3, timed(event_pb2.Event(setup_output=event_pb2.SetupOutput(stderr=b"\xff\x00bad")), 2, 123_456_000)
                ),
                entry(4, timed(event_pb2.Event(setup_output=event_pb2.SetupOutput(stdout=b"")), 3, 1)),
                entry(5, timed(event_pb2.Event(setup_finished=event_pb2.SetupFinished(exit_code=7)), 4)),
                entry(6, timed(event_pb2.Event(harness_launch_failed=event_pb2.HarnessLaunchFailed(reason="boom")), 5)),
                entry(
                    7, timed(event_pb2.Event(harness_started=event_pb2.HarnessStarted(resumed=True, pid=42)), 6), 3, 4
                ),
                entry(
                    8, timed(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="model-a")), 7)
                ),
                entry(
                    9, event_pb2.Event(native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="{}"))
                ),
                entry(10, event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="warning"))),
                entry(11, event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="here"))),
                entry(
                    12,
                    timed(
                        event_pb2.Event(
                            turn_completed=event_pb2.TurnCompleted(
                                turn_id="turn-1", status=UNKNOWN_TURN_STATUS, error="x", ended_by_teardown=True
                            )
                        ),
                        8,
                    ),
                ),
                entry(
                    13,
                    timed(
                        event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=3, stopped_by_runner=True)), 9
                    ),
                ),
                entry(14, timed(event_pb2.Event(harness_lost=event_pb2.HarnessLost()), 10)),
                entry(15, timed(event_pb2.Event(setup_interrupted=event_pb2.SetupInterrupted()), 11)),
                entry(
                    16, event_pb2.Event(journal_reopened=event_pb2.JournalReopened(incarnation_uid="test-incarnation"))
                ),
            ),
        ),
        Scenario(
            "controls follow turns, models and the harness",
            single(
                entry(1, event_pb2.Event(harness_started=event_pb2.HarnessStarted())),
                entry(2, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1", model="model-a"))),
                entry(3, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-2"))),
                entry(4, event_pb2.Event(turn_completed=event_pb2.TurnCompleted(turn_id="turn-1"))),
                entry(5, event_pb2.Event(model_changed=event_pb2.ModelChanged(model="model-b"))),
                entry(
                    6,
                    event_pb2.Event(
                        reasoning_effort_changed=event_pb2.ReasoningEffortChanged(previous_effort="low", effort="high")
                    ),
                ),
                text_delta(7, "answer", "hi"),
                entry(8, event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=0))),
                text_delta(9, "later", "after the harness stopped"),
                entry(10, event_pb2.Event(harness_lost=event_pb2.HarnessLost())),
            ),
        ),
    ]


def command_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "commands settle as failed, noop and effected",
            single(
                submit_input(1, "failing", "do it"),
                submit_input(2, "idle", "again"),
                change_model(3, "pending-model", "model-x"),
                admitted(
                    4,
                    command_pb2.Command(
                        command_id="effort", change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort="low")
                    ),
                ),
                admitted(
                    5,
                    command_pb2.Command(command_id="stop", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn-1")),
                ),
                entry(6, event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn-1"))),
                entry(
                    7,
                    event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="failing", reason="no harness")),
                ),
                entry(8, event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="idle"))),
                entry(
                    9,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(
                            turn_id="turn-1", status=event_pb2.TURN_STATUS_INTERRUPTED, interrupted_by_command_id="stop"
                        )
                    ),
                ),
            ),
        ),
        Scenario(
            "a confirmed input without a harness message id or turn",
            single(
                submit_input(1, "input", "hello"),
                entry(
                    2,
                    event_pb2.Event(
                        harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                            text="hello", origin_command_ids=["input"]
                        )
                    ),
                    7,
                ),
                entry(
                    3,
                    event_pb2.Event(
                        harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                            harness_message_id="typed-in-terminal", text="not from a command"
                        )
                    ),
                ),
            ),
        ),
    ]


def settled_range_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "entries skip settled and removed ranges",
            (
                Batch((text_delta(1, "answer", "a"),)),
                Batch((text_delta(5, "answer", "b"), text_delta(8, "answer", "c")), settled=((6, 7), (2, 4))),
                Batch((text_completed(9, "answer", "abc"),)),
            ),
        ),
        Scenario(
            "the feed state of a batch can be failed",
            (
                Batch((text_delta(1, "answer", "a"),)),
                Batch(
                    (text_delta(2, "answer", "b"),),
                    operational=ThreadOperationalState(
                        status="failed",
                        last_verified_cursor="2",
                        feed_error=ThreadFeedErrorState(cursor="2", message="Session history projection stalled."),
                    ),
                ),
                Batch(
                    (text_delta(3, "answer", "c"),),
                    operational=ThreadOperationalState(
                        status="ended",
                        last_verified_cursor="3",
                        feed_error=ThreadFeedErrorState(cursor=None, message="gone"),
                    ),
                ),
            ),
        ),
    ]


def error_scenarios() -> list[Scenario]:
    start = text_delta(1, "answer", "a")
    tool = item_started(2, "tool", event_pb2.ITEM_KIND_TOOL_CALL)
    return [
        Scenario("an item of an unknown kind", single(start, item_started(2, "other", UNKNOWN_ITEM_KIND))),
        Scenario(
            "an item completed without an outcome",
            single(start, entry(2, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="answer")))),
        ),
        Scenario("an observation this fold does not know", single(start, entry(2, event_pb2.Event()))),
        Scenario(
            "a command admitted without an operation",
            single(start, admitted(2, command_pb2.Command(command_id="blank"))),
        ),
        Scenario(
            "a recovery of an unknown disposition",
            single(start, tool, reconciled(3, "x", recovery(UNKNOWN_DISPOSITION))),
        ),
        Scenario(
            "a revised recovery without replacement",
            single(
                start,
                tool,
                reconciled(
                    3, "x", event_pb2.ItemRecovery(item_id="tool", disposition=event_pb2.RECOVERY_DISPOSITION_REVISED)
                ),
            ),
        ),
        Scenario(
            "a replacement on a recovery that is not revised",
            single(
                start,
                tool,
                reconciled(
                    3,
                    "x",
                    event_pb2.ItemRecovery(
                        item_id="tool",
                        disposition=event_pb2.RECOVERY_DISPOSITION_RETAINED,
                        replacement=event_pb2.RecoveredContent(output="x"),
                    ),
                ),
            ),
        ),
        Scenario("a command admitted twice", single(submit_input(1, "input", "a"), submit_input(2, "input", "b"))),
        Scenario(
            "a command settled twice",
            single(
                submit_input(1, "input", "a"),
                entry(2, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="input", reason="x"))),
                entry(3, event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="input", reason="y"))),
            ),
        ),
        Scenario(
            "a command settled that was never admitted",
            single(entry(1, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="ghost", reason="x")))),
        ),
        Scenario("a skipped cursor", whole(start, text_delta(3, "answer", "c"))),
        Scenario(
            "an entry from another source",
            whole(
                start,
                entry(
                    2, event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="answer", text="b")), source_id="other"
                ),
            ),
        ),
        Scenario("an item without an identity", single(start, text_delta(2, "", "orphan"))),
        Scenario(
            "a failure partway through a batch leaves no rows",
            whole(start, tool, entry(3, event_pb2.Event(command_noop=event_pb2.CommandNoop(command_id="ghost")))),
        ),
    ]


def build() -> Json:
    entries = conversation()
    scenarios = [
        Scenario("a conversation in one batch", whole(*entries)),
        Scenario("a conversation one entry per batch", single(*entries)),
        *authoritative_scenarios(),
        *reconciliation_scenarios(),
        *lifecycle_scenarios(),
        *command_scenarios(),
        *settled_range_scenarios(),
        *error_scenarios(),
    ]
    return {
        "thread_id": str(THREAD_ID),
        "source_id": SOURCE,
        "projection_epoch": EPOCH,
        "scenarios": [run(scenario) for scenario in scenarios],
    }


def serialize(golden: Json) -> str:
    return json.dumps(golden, indent=1, sort_keys=True) + "\n"


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
