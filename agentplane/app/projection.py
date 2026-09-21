"""Incremental server projection from committed source observations.

The persistence worker loads the batch's item/command keys, including explicit absence,
then commits entity upserts, payload writes and ViewState atomically under a checkpoint
compare-and-swap. Historical payload bytes are never needed by this fold.
"""

from dataclasses import dataclass
from enum import StrEnum

from agentplane.app import thread_view_pb2
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf

CARRIED = frozenset(
    {"turn_started", "turn_completed", "model_changed", "harness_started", "harness_exited", "harness_lost"}
)
SILENT = frozenset({"native", "debug_checkpoint", "harness_stderr"})


class OperationKind(StrEnum):
    SUBMIT_INPUT = "submit_input"
    CHANGE_MODEL = "change_model"
    INTERRUPT_TURN = "interrupt_turn"
    STOP_RUNNER_SESSION = "stop_runner_session"


class UninterpretedObservationError(ValueError):
    def __init__(self, cursor: int, case: str) -> None:
        super().__init__(f"uninterpreted observation {case!r} at {cursor=}")
        self.cursor = cursor
        self.case = case


@dataclass(frozen=True)
class EventBatch:
    # This projector consumes the exact original runner archive, not imported journals.
    source_id: str
    after_cursor: int
    entries: tuple[event_log_pb2.EventEntry, ...]


@dataclass(frozen=True)
class PriorEntities:
    # A missing key is a worker error. None is an indexed lookup proving first observation.
    items: dict[str, thread_view_pb2.Segment | None]
    commands: dict[str, thread_view_pb2.CommandSummary | None]


@dataclass(frozen=True)
class AppendPayload:
    base: thread_view_pb2.PayloadRef | None
    reference: thread_view_pb2.PayloadRef
    text: str


@dataclass(frozen=True)
class ReplacePayload:
    reference: thread_view_pb2.PayloadRef
    text: str


@dataclass(frozen=True)
class ProjectionBatch:
    state: thread_view_pb2.ViewState
    segment_upserts: tuple[thread_view_pb2.Segment, ...]
    command_upserts: tuple[thread_view_pb2.CommandSummary, ...]
    # Ordered writes can reference an earlier write within this batch.
    payload_writes: tuple[AppendPayload | ReplacePayload, ...]


def empty(source_id: str, projection_epoch: str) -> thread_view_pb2.ViewState:
    if not source_id or not projection_epoch:
        raise ValueError("a projection needs both identities")
    return thread_view_pb2.ViewState(
        position=thread_view_pb2.Position(source_id=source_id, projection_epoch=projection_epoch)
    )


class _Builder:
    def __init__(self, state: thread_view_pb2.ViewState, prior: PriorEntities) -> None:
        self.state = thread_view_pb2.ViewState()
        self.state.CopyFrom(state)
        self.prior = prior
        self.prior_through_cursor = state.position.through_cursor
        self.items: dict[str, thread_view_pb2.Segment] = {}
        self.segments: dict[int, thread_view_pb2.Segment] = {}
        self.commands: dict[str, thread_view_pb2.CommandSummary] = {}
        self.payloads: list[AppendPayload | ReplacePayload] = []

    def item(self, cursor: int, item_id: str) -> thread_view_pb2.Segment:
        if not item_id:
            raise ValueError("empty item identity")
        if item_id not in self.items:
            if item_id not in self.prior.items:
                raise ValueError(f"missing prior item lookup: {item_id}")
            prior = self.prior.items[item_id]
            segment = thread_view_pb2.Segment()
            if prior is None:
                segment.cursor = cursor
                segment.item.item_id = item_id
                if self.state.controls.HasField("active_turn_id"):
                    segment.item.turn_id = self.state.controls.active_turn_id
            else:
                if (
                    prior.WhichOneof("content") != "item"
                    or prior.item.item_id != item_id
                    or not 0 < prior.cursor <= prior.revision_cursor <= self.prior_through_cursor
                ):
                    raise ValueError(f"invalid prior item: {item_id}")
                for field, expected in (
                    (prior.item.text, thread_view_pb2.PAYLOAD_FIELD_TEXT),
                    (prior.item.arguments_json, thread_view_pb2.PAYLOAD_FIELD_ARGUMENTS),
                    (prior.item.output, thread_view_pb2.PAYLOAD_FIELD_OUTPUT),
                ):
                    if field.HasField("value"):
                        raise ValueError(f"prior item must omit payload bodies: {item_id}")
                    if field.HasField("reference"):
                        self.validate_reference(field.reference, prior.cursor, expected)
                segment.CopyFrom(prior)
            self.items[item_id] = segment
            self.segments[segment.cursor] = segment
        segment = self.items[item_id]
        segment.revision_cursor = cursor
        return segment

    def validate_reference(
        self, reference: thread_view_pb2.PayloadRef, owner_cursor: int, field: thread_view_pb2.PayloadField
    ) -> None:
        position = self.state.position
        if (
            reference.source_id != position.source_id
            or reference.projection_epoch != position.projection_epoch
            or reference.owner_cursor != owner_cursor
            or reference.field != field
            or not owner_cursor <= reference.revision_cursor <= self.prior_through_cursor
        ):
            raise ValueError("payload reference does not belong to the projected prefix")

    def content(
        self,
        target: thread_view_pb2.Text,
        owner: int,
        cursor: int,
        field: thread_view_pb2.PayloadField,
        text: str,
        *,
        append: bool = False,
    ) -> None:
        reference = thread_view_pb2.PayloadRef(
            source_id=self.state.position.source_id,
            projection_epoch=self.state.position.projection_epoch,
            owner_cursor=owner,
            field=field,
            revision_cursor=cursor,
        )
        if append:
            base = None
            if target.HasField("reference"):
                base = thread_view_pb2.PayloadRef()
                base.CopyFrom(target.reference)
            self.payloads.append(AppendPayload(base=base, reference=reference, text=text))
        else:
            self.payloads.append(ReplacePayload(reference=reference, text=text))
        target.CopyFrom(thread_view_pb2.Text(reference=reference))

    def admit(self, cursor: int, command: command_pb2.Command) -> None:
        command_id = command.command_id
        if not command_id or command_id not in self.prior.commands:
            raise ValueError(f"missing prior command lookup: {command_id}")
        if command_id in self.commands or self.prior.commands[command_id] is not None:
            raise ValueError(f"duplicate command admission: {command_id}")
        operation = command.WhichOneof("operation")
        if operation is None:
            raise UninterpretedObservationError(cursor, "command_admitted.operation")
        summary = thread_view_pb2.CommandSummary(
            command_id=command_id,
            operation_kind=OperationKind(operation),
            admission_cursor=cursor,
            pending=thread_view_pb2.Pending(),
        )
        if operation == OperationKind.SUBMIT_INPUT:
            self.content(
                summary.input, cursor, cursor, thread_view_pb2.PAYLOAD_FIELD_COMMAND_INPUT, command.submit_input.text
            )
        self.commands[command_id] = summary
        self.state.unresolved_count += 1

    def settle(
        self,
        command_id: str,
        outcome: thread_view_pb2.Effected | thread_view_pb2.CommandFailure | thread_view_pb2.CommandNoop,
    ) -> None:
        if command_id not in self.commands:
            prior = self.prior.commands.get(command_id)
            if prior is None:
                raise ValueError(f"missing admitted command: {command_id}")
            if prior.command_id != command_id or not 0 < prior.admission_cursor <= self.prior_through_cursor:
                raise ValueError(f"invalid prior command: {command_id}")
            if prior.input.HasField("value"):
                raise ValueError(f"prior command must omit payload bodies: {command_id}")
            if prior.input.HasField("reference"):
                self.validate_reference(
                    prior.input.reference, prior.admission_cursor, thread_view_pb2.PAYLOAD_FIELD_COMMAND_INPUT
                )
            summary = thread_view_pb2.CommandSummary()
            summary.CopyFrom(prior)
            self.commands[command_id] = summary
        summary = self.commands[command_id]
        if summary.WhichOneof("outcome") != "pending":
            raise ValueError(f"command already settled: {command_id}")
        if self.state.unresolved_count == 0:
            raise ValueError("unresolved command count underflow")
        match outcome:
            case thread_view_pb2.Effected():
                summary.effected.CopyFrom(outcome)
            case thread_view_pb2.CommandFailure():
                summary.failed.CopyFrom(outcome)
            case thread_view_pb2.CommandNoop():
                summary.noop.CopyFrom(outcome)
        self.state.unresolved_count -= 1

    def effected(self, cursor: int, command_id: str) -> None:
        self.settle(command_id, thread_view_pb2.Effected(origin_cursor=cursor))

    def fold(self, entry: event_log_pb2.EventEntry) -> None:
        cursor, event = entry.cursor, entry.event
        case = event.WhichOneof("observation")
        match case:
            case "item_started":
                observed = event.item_started
                segment = self.item(cursor, observed.item_id)
                segment.item.kind = observed.kind
                segment.item.tool_name = observed.tool_name
            case "text_delta":
                segment = self.item(cursor, event.text_delta.item_id)
                self.content(
                    segment.item.text,
                    segment.cursor,
                    cursor,
                    thread_view_pb2.PAYLOAD_FIELD_TEXT,
                    event.text_delta.text,
                    append=True,
                )
            case "tool_arguments_delta":
                segment = self.item(cursor, event.tool_arguments_delta.item_id)
                self.content(
                    segment.item.arguments_json,
                    segment.cursor,
                    cursor,
                    thread_view_pb2.PAYLOAD_FIELD_ARGUMENTS,
                    event.tool_arguments_delta.partial_json,
                    append=True,
                )
            case "tool_arguments":
                segment = self.item(cursor, event.tool_arguments.item_id)
                self.content(
                    segment.item.arguments_json,
                    segment.cursor,
                    cursor,
                    thread_view_pb2.PAYLOAD_FIELD_ARGUMENTS,
                    event.tool_arguments.arguments_json,
                )
            case "tool_output_delta":
                segment = self.item(cursor, event.tool_output_delta.item_id)
                self.content(
                    segment.item.output,
                    segment.cursor,
                    cursor,
                    thread_view_pb2.PAYLOAD_FIELD_OUTPUT,
                    event.tool_output_delta.text,
                    append=True,
                )
            case "item_completed":
                completed = event.item_completed
                segment = self.item(cursor, completed.item_id)
                match completed.WhichOneof("outcome"):
                    case "text":
                        self.content(
                            segment.item.text,
                            segment.cursor,
                            cursor,
                            thread_view_pb2.PAYLOAD_FIELD_TEXT,
                            completed.text,
                        )
                        segment.item.completed_text.SetInParent()
                    case "tool":
                        self.content(
                            segment.item.output,
                            segment.cursor,
                            cursor,
                            thread_view_pb2.PAYLOAD_FIELD_OUTPUT,
                            completed.tool.output,
                        )
                        segment.item.completed_tool.succeeded = completed.tool.succeeded
                    case _:
                        raise UninterpretedObservationError(cursor, "item_completed.outcome")
            case "harness_user_message_confirmed":
                confirmed = event.harness_user_message_confirmed
                segment = thread_view_pb2.Segment(
                    cursor=cursor,
                    revision_cursor=cursor,
                    confirmed_input=thread_view_pb2.ConfirmedInput(
                        harness_message_id=confirmed.harness_message_id, origin_command_ids=confirmed.origin_command_ids
                    ),
                )
                if confirmed.turn_id:
                    segment.confirmed_input.turn_id = confirmed.turn_id
                self.content(
                    segment.confirmed_input.text,
                    cursor,
                    cursor,
                    thread_view_pb2.PAYLOAD_FIELD_CONFIRMED_INPUT,
                    confirmed.text,
                )
                self.segments[cursor] = segment
                for command_id in confirmed.origin_command_ids:
                    self.effected(cursor, command_id)
            case "command_admitted":
                self.admit(cursor, event.command_admitted.command)
            case "command_failed":
                self.settle(
                    event.command_failed.command_id,
                    thread_view_pb2.CommandFailure(origin_cursor=cursor, reason=event.command_failed.reason),
                )
            case "command_noop":
                self.settle(
                    event.command_noop.command_id,
                    thread_view_pb2.CommandNoop(origin_cursor=cursor, reason=event.command_noop.reason),
                )
            case _ if case in SILENT:
                pass
            case _ if case in CARRIED:
                self.segments[cursor] = thread_view_pb2.Segment(cursor=cursor, revision_cursor=cursor, event=event)
                self.carry_controls(cursor, event)
            case _:
                raise UninterpretedObservationError(cursor, str(case))

    def carry_controls(self, cursor: int, event: event_pb2.Event) -> None:
        controls = self.state.controls
        match event.WhichOneof("observation"):
            case "turn_started":
                controls.active_turn_id = event.turn_started.turn_id
                if event.turn_started.model:
                    controls.applied_model = event.turn_started.model
            case "turn_completed":
                completed = event.turn_completed
                if controls.active_turn_id == completed.turn_id:
                    controls.ClearField("active_turn_id")
                if completed.interrupted_by_command_id:
                    self.effected(cursor, completed.interrupted_by_command_id)
            case "model_changed":
                controls.applied_model = event.model_changed.model
                if event.model_changed.command_id:
                    self.effected(cursor, event.model_changed.command_id)
            case "harness_started":
                controls.harness_state = thread_view_pb2.HARNESS_STATE_RUNNING
            case "harness_exited":
                controls.harness_state = thread_view_pb2.HARNESS_STATE_STOPPED
                controls.ClearField("active_turn_id")
                if event.harness_exited.stopped_by_command_id:
                    self.effected(cursor, event.harness_exited.stopped_by_command_id)
            case "harness_lost":
                controls.harness_state = thread_view_pb2.HARNESS_STATE_LOST
                controls.ClearField("active_turn_id")


def advance(state: thread_view_pb2.ViewState, batch: EventBatch, prior: PriorEntities) -> ProjectionBatch:
    position = state.position
    if not position.source_id or not position.projection_epoch:
        raise ValueError("a projection needs both identities")
    if batch.source_id != position.source_id:
        raise ValueError("batch source does not match projection")
    if batch.after_cursor != position.through_cursor:
        raise ValueError("batch checkpoint does not match projection")
    builder = _Builder(state, prior)
    for entry in batch.entries:
        if entry.cursor != builder.state.position.through_cursor + 1:
            raise ValueError(f"noncontiguous source cursor: {entry.cursor}")
        if entry.origin.source_id != batch.source_id or entry.origin.sequence != entry.cursor:
            raise ValueError("entry does not belong to the original source archive")
        builder.fold(entry)
        builder.state.position.through_cursor = entry.cursor
    return ProjectionBatch(
        state=builder.state,
        segment_upserts=tuple(builder.segments.values()),
        command_upserts=tuple(builder.commands.values()),
        payload_writes=tuple(builder.payloads),
    )
