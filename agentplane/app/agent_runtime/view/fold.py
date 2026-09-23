"""Pure incremental fold of a Thread's event log into its read model.

The module has no database, browser, or runtime integration.  It returns typed domain
records and logical payload write intents for a later transactional storage worker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from agentplane.protocol import command_pb2, event_log_pb2, event_pb2


class ObservationNotUnderstoodError(ValueError):
    """This fold does not model an observation the runner recorded."""

    def __init__(self, cursor: int, observation: str) -> None:
        self.cursor = cursor
        super().__init__(f"uninterpreted semantic observation {observation!r}")


class FoldContractError(ValueError):
    """The batch, or the rows preloaded for it, break the fold's contract: a caller bug, not a newer runner."""

    def __init__(self, cursor: int, message: str) -> None:
        self.cursor = cursor
        super().__init__(message)


class PayloadField(StrEnum):
    TEXT = "text"
    ARGUMENTS = "arguments"
    OUTPUT = "output"
    CONFIRMED_INPUT = "confirmed_input"
    COMMAND_INPUT = "command_input"


class CommandOutcome(StrEnum):
    PENDING = "pending"
    EFFECTED = "effected"
    FAILED = "failed"
    NOOP = "noop"


@dataclass(frozen=True)
class Position:
    source_id: str
    projection_epoch: str
    through_cursor: int = 0


@dataclass(frozen=True)
class PayloadRef:
    projection_epoch: str
    owner_cursor: int
    owner_id: str
    field: PayloadField
    revision_cursor: int
    generation: int


@dataclass(frozen=True)
class PayloadOwner:
    """Identity shared by an item field, command input, or confirmed input."""

    projection_epoch: str
    cursor: int
    owner_id: str
    revision_cursor: int


@dataclass(frozen=True)
class TextCompletion:
    pass


@dataclass(frozen=True)
class ToolCompletion:
    succeeded: bool


Completion = TextCompletion | ToolCompletion


@dataclass(frozen=True)
class Item:
    projection_epoch: str
    item_id: str
    cursor: int
    revision_cursor: int
    kind: int = event_pb2.ITEM_KIND_UNSPECIFIED
    tool_name: str = ""
    turn_id: str | None = None
    text: PayloadRef | None = None
    arguments: PayloadRef | None = None
    output: PayloadRef | None = None
    completion: Completion | None = None

    def payload(self, field: PayloadField) -> PayloadRef | None:
        match field:
            case PayloadField.TEXT:
                return self.text
            case PayloadField.ARGUMENTS:
                return self.arguments
            case PayloadField.OUTPUT:
                return self.output
        raise ValueError(f"field {field} does not belong to a thread item")


@dataclass(frozen=True)
class ConfirmedInput:
    projection_epoch: str
    cursor: int
    revision_cursor: int
    harness_message_id: str
    origin_command_ids: tuple[str, ...]
    turn_id: str | None
    text: PayloadRef


@dataclass(frozen=True)
class LifecycleSegment:
    projection_epoch: str
    cursor: int
    revision_cursor: int
    observation: str
    event: event_pb2.Event


@dataclass(frozen=True)
class CommandSummary:
    projection_epoch: str
    command_id: str
    admission_cursor: int
    operation: str
    outcome: CommandOutcome = CommandOutcome.PENDING
    outcome_cursor: int | None = None
    outcome_reason: str | None = None
    input: PayloadRef | None = None


@dataclass(frozen=True)
class Controls:
    applied_model: str | None = None
    active_turn_id: str | None = None
    harness_state: str | None = None


@dataclass(frozen=True)
class ViewState:
    position: Position
    controls: Controls = Controls()


@dataclass(frozen=True)
class EvidenceAssociation:
    projection_epoch: str
    entity_cursor: int
    observation_cursor: int
    source_sequences: tuple[int, ...]


@dataclass(frozen=True)
class AppendPayload:
    base: PayloadRef | None
    reference: PayloadRef
    text: str


@dataclass(frozen=True)
class ReplacePayload:
    reference: PayloadRef
    text: str


PayloadWrite = AppendPayload | ReplacePayload


@dataclass(frozen=True)
class PriorEntities:
    """Rows preloaded only for ``touched_keys``; missing differs from proved absence."""

    items: dict[str, Item | None]
    commands: dict[str, CommandSummary | None]
    # The whole text each preloaded item holds in a field of `TouchedKeys.completed`.
    bodies: dict[PayloadRef, str]


@dataclass(frozen=True)
class EventBatch:
    source_id: str
    after_cursor: int
    entries: tuple[event_log_pb2.EventEntry, ...]


@dataclass(frozen=True)
class TouchedKeys:
    item_ids: frozenset[str]
    command_ids: frozenset[str]
    # Item fields an authoritative value completes, which the fold compares with the stored body.
    completed: frozenset[tuple[str, PayloadField]]


@dataclass(frozen=True)
class ProjectionBatch:
    state: ViewState
    item_upserts: tuple[Item, ...]
    confirmed_input_upserts: tuple[ConfirmedInput, ...]
    lifecycle_upserts: tuple[LifecycleSegment, ...]
    command_upserts: tuple[CommandSummary, ...]
    evidence_upserts: tuple[EvidenceAssociation, ...]
    payload_writes: tuple[PayloadWrite, ...]


_SILENT = frozenset({"native", "harness_stderr", "debug_checkpoint"})
_LIFECYCLE = frozenset(
    {"turn_started", "turn_completed", "model_changed", "harness_started", "harness_exited", "harness_lost"}
)


def initial(source_id: str, projection_epoch: str) -> ViewState:
    if not source_id or not projection_epoch:
        raise ValueError("projection source and epoch are required")
    return ViewState(Position(source_id, projection_epoch))


def touched_keys(batch: EventBatch) -> TouchedKeys:
    """Discover all rows a worker needs before folding a batch."""

    items: set[str] = set()
    commands: set[str] = set()
    completed: set[tuple[str, PayloadField]] = set()
    for entry in batch.entries:
        event = entry.event
        match event.WhichOneof("observation"):
            case "item_started":
                items.add(event.item_started.item_id)
            case "text_delta":
                items.add(event.text_delta.item_id)
            case "tool_arguments_delta":
                items.add(event.tool_arguments_delta.item_id)
            case "tool_arguments":
                items.add(event.tool_arguments.item_id)
                completed.add((event.tool_arguments.item_id, PayloadField.ARGUMENTS))
            case "tool_output_delta":
                items.add(event.tool_output_delta.item_id)
            case "item_completed":
                items.add(event.item_completed.item_id)
                match event.item_completed.WhichOneof("outcome"):
                    case "text":
                        completed.add((event.item_completed.item_id, PayloadField.TEXT))
                    case "tool":
                        completed.add((event.item_completed.item_id, PayloadField.OUTPUT))
            case "command_admitted":
                commands.add(event.command_admitted.command.command_id)
            case "command_failed":
                commands.add(event.command_failed.command_id)
            case "command_noop":
                commands.add(event.command_noop.command_id)
            case "harness_user_message_confirmed":
                commands.update(event.harness_user_message_confirmed.origin_command_ids)
            case "turn_completed":
                commands.add(event.turn_completed.interrupted_by_command_id)
            case "model_changed":
                commands.add(event.model_changed.command_id)
            case "harness_exited":
                commands.add(event.harness_exited.stopped_by_command_id)
    return TouchedKeys(
        frozenset(items - {""}), frozenset(commands - {""}), frozenset(key for key in completed if key[0])
    )


class _Fold:
    def __init__(self, state: ViewState, prior: PriorEntities) -> None:
        self.state = replace(state, position=replace(state.position), controls=replace(state.controls))
        self.initial_through_cursor = state.position.through_cursor
        self.prior = prior
        self.items: dict[str, Item] = {}
        self.commands: dict[str, CommandSummary] = {}
        self.confirmed: dict[int, ConfirmedInput] = {}
        self.lifecycle: dict[int, LifecycleSegment] = {}
        self.evidence: list[EvidenceAssociation] = []
        self.payload_writes: dict[PayloadRef, PayloadWrite] = {}

    @staticmethod
    def _item_owner(item: Item) -> PayloadOwner:
        return PayloadOwner(item.projection_epoch, item.cursor, item.item_id, item.revision_cursor)

    def _validate_ref(
        self, reference: PayloadRef, owner: PayloadOwner, field: PayloadField, *, preloaded: bool
    ) -> None:
        position = self.state.position
        if (
            reference.projection_epoch != position.projection_epoch
            or reference.owner_cursor != owner.cursor
            or reference.owner_id != owner.owner_id
            or reference.field is not field
            or not owner.cursor <= reference.generation <= reference.revision_cursor <= owner.revision_cursor
            or (preloaded and reference.revision_cursor > self.initial_through_cursor)
        ):
            raise ValueError("payload reference does not belong to its owner revision")

    def _item(self, cursor: int, item_id: str) -> Item:
        if not item_id:
            raise ValueError("empty item identity")
        if item_id in self.items:
            return self.items[item_id]
        if item_id not in self.prior.items:
            raise ValueError(f"missing prior item lookup: {item_id}")
        prior = self.prior.items[item_id]
        position = self.state.position
        if prior is None:
            item = Item(position.projection_epoch, item_id, cursor, cursor, turn_id=self.state.controls.active_turn_id)
        else:
            if (
                prior.projection_epoch != position.projection_epoch
                or prior.item_id != item_id
                or not 0 < prior.cursor <= prior.revision_cursor <= self.initial_through_cursor
            ):
                raise ValueError(f"invalid prior item: {item_id}")
            owner = self._item_owner(prior)
            for field, value in (
                (PayloadField.TEXT, prior.text),
                (PayloadField.ARGUMENTS, prior.arguments),
                (PayloadField.OUTPUT, prior.output),
            ):
                if value is not None:
                    self._validate_ref(value, owner, field, preloaded=True)
            item = replace(prior)
        self.items[item_id] = item
        return item

    def _save_item(self, item: Item, cursor: int) -> Item:
        item = replace(item, revision_cursor=cursor)
        self.items[item.item_id] = item
        return item

    def _write(
        self, owner: PayloadOwner, base: PayloadRef | None, cursor: int, field: PayloadField, text: str, *, append: bool
    ) -> PayloadRef:
        if base is not None:
            self._validate_ref(base, owner, field, preloaded=False)
        reference = PayloadRef(
            self.state.position.projection_epoch,
            owner.cursor,
            owner.owner_id,
            field,
            cursor,
            base.generation if append and base else cursor,
        )
        self.payload_writes[reference] = (
            AppendPayload(base, reference, text) if append else ReplacePayload(reference, text)
        )
        return reference

    def _body(self, reference: PayloadRef) -> str:
        """A reference's whole text: this batch's appends back to its generation's first write, or
        to the body preloaded for an earlier batch's revision."""
        fragments: list[str] = []
        while isinstance(write := self.payload_writes.get(reference), AppendPayload) and write.base is not None:
            fragments.append(write.text)
            reference = write.base
        if write is not None:
            fragments.append(write.text)
        elif reference in self.prior.bodies:
            fragments.append(self.prior.bodies[reference])
        else:
            raise ValueError(f"missing prior body lookup: {reference.owner_id} {reference.field}")
        return "".join(reversed(fragments))

    def _item_write(self, cursor: int, item_id: str, field: PayloadField, text: str, *, append: bool) -> Item:
        item = self._item(cursor, item_id)
        value = self._write(self._item_owner(item), item.payload(field), cursor, field, text, append=append)
        match field:
            case PayloadField.TEXT:
                item = replace(item, text=value)
            case PayloadField.ARGUMENTS:
                item = replace(item, arguments=value)
            case PayloadField.OUTPUT:
                item = replace(item, output=value)
        return self._save_item(item, cursor)

    def _complete(self, cursor: int, item_id: str, field: PayloadField, text: str) -> Item:
        """An authoritative value replaces the field's body, unless it is the text the body holds:
        then only the revision moves, so a reader holding the body has nothing to fetch."""
        base = self._item(cursor, item_id).payload(field)
        if base is not None and self._body(base) == text:
            return self._item_write(cursor, item_id, field, "", append=True)
        return self._item_write(cursor, item_id, field, text, append=False)

    def _evidence(self, item: Item | int, entry: event_log_pb2.EventEntry) -> None:
        self.evidence.append(
            EvidenceAssociation(
                self.state.position.projection_epoch,
                item.cursor if isinstance(item, Item) else item,
                entry.cursor,
                tuple(entry.event.source_sequences),
            )
        )

    def _command(self, command_id: str) -> CommandSummary:
        if command_id in self.commands:
            return self.commands[command_id]
        if command_id not in self.prior.commands:
            raise ValueError(f"missing prior command lookup: {command_id}")
        prior = self.prior.commands[command_id]
        if prior is None:
            raise ValueError(f"missing admitted command: {command_id}")
        position = self.state.position
        if (
            prior.projection_epoch != position.projection_epoch
            or prior.command_id != command_id
            or not 0 < prior.admission_cursor <= self.initial_through_cursor
        ):
            raise ValueError(f"invalid prior command: {command_id}")
        if (reference := prior.input) is not None:
            owner = PayloadOwner(prior.projection_epoch, prior.admission_cursor, command_id, prior.admission_cursor)
            try:
                self._validate_ref(reference, owner, PayloadField.COMMAND_INPUT, preloaded=True)
            except ValueError as error:
                raise ValueError(f"invalid prior command input: {command_id}") from error
            if reference.revision_cursor != prior.admission_cursor:
                raise ValueError(f"invalid prior command input: {command_id}")
        self.commands[command_id] = replace(prior)
        return self.commands[command_id]

    def _admit(self, cursor: int, command: command_pb2.Command) -> None:
        command_id = command.command_id
        if not command_id or command_id not in self.prior.commands:
            raise ValueError(f"missing prior command lookup: {command_id}")
        if command_id in self.commands or self.prior.commands[command_id] is not None:
            raise ValueError(f"duplicate command admission: {command_id}")
        operation = command.WhichOneof("operation")
        if operation is None:
            raise ObservationNotUnderstoodError(cursor, "command_admitted.operation")
        summary = CommandSummary(self.state.position.projection_epoch, command_id, cursor, operation)
        if operation == "submit_input":
            owner = PayloadOwner(summary.projection_epoch, cursor, command_id, cursor)
            summary = replace(
                summary,
                input=self._write(
                    owner, None, cursor, PayloadField.COMMAND_INPUT, command.submit_input.text, append=False
                ),
            )
        self.commands[command_id] = summary

    def _settle(
        self, entry: event_log_pb2.EventEntry, command_id: str, outcome: CommandOutcome, reason: str | None = None
    ) -> None:
        cursor = entry.cursor
        summary = self._command(command_id)
        if summary.outcome is not CommandOutcome.PENDING:
            raise ValueError(f"command already settled: {command_id}")
        self.commands[command_id] = replace(summary, outcome=outcome, outcome_cursor=cursor, outcome_reason=reason)
        self._evidence(summary.admission_cursor, entry)

    def _lifecycle(self, entry: event_log_pb2.EventEntry, observation: str) -> None:
        cursor, event = entry.cursor, entry.event
        self.lifecycle[cursor] = LifecycleSegment(
            self.state.position.projection_epoch,
            cursor,
            cursor,
            observation,
            event_pb2.Event.FromString(event.SerializeToString()),
        )
        controls = self.state.controls
        match observation:
            case "turn_started":
                controls = replace(
                    controls,
                    active_turn_id=event.turn_started.turn_id,
                    applied_model=event.turn_started.model or controls.applied_model,
                )
            case "turn_completed":
                if event.turn_completed.interrupted_by_command_id:
                    self._settle(entry, event.turn_completed.interrupted_by_command_id, CommandOutcome.EFFECTED)
                if controls.active_turn_id == event.turn_completed.turn_id:
                    controls = replace(controls, active_turn_id=None)
            case "model_changed":
                if event.model_changed.command_id:
                    self._settle(entry, event.model_changed.command_id, CommandOutcome.EFFECTED)
                controls = replace(controls, applied_model=event.model_changed.model)
            case "harness_started":
                controls = replace(controls, harness_state="running")
            case "harness_exited":
                if event.harness_exited.stopped_by_command_id:
                    self._settle(entry, event.harness_exited.stopped_by_command_id, CommandOutcome.EFFECTED)
                controls = replace(controls, harness_state="stopped", active_turn_id=None)
            case "harness_lost":
                controls = replace(controls, harness_state="lost", active_turn_id=None)
        self.state = replace(self.state, controls=controls)

    def fold(self, entry: event_log_pb2.EventEntry) -> None:
        cursor, event = entry.cursor, entry.event
        observation = event.WhichOneof("observation")
        match observation:
            case "item_started":
                if event.item_started.kind not in {
                    event_pb2.ITEM_KIND_ASSISTANT_TEXT,
                    event_pb2.ITEM_KIND_REASONING,
                    event_pb2.ITEM_KIND_TOOL_CALL,
                }:
                    raise ObservationNotUnderstoodError(cursor, "item_started.kind")
                item = self._item(cursor, event.item_started.item_id)
                self._evidence(
                    self._save_item(
                        replace(item, kind=event.item_started.kind, tool_name=event.item_started.tool_name), cursor
                    ),
                    entry,
                )
            case "text_delta":
                self._evidence(
                    self._item_write(
                        cursor, event.text_delta.item_id, PayloadField.TEXT, event.text_delta.text, append=True
                    ),
                    entry,
                )
            case "tool_arguments_delta":
                self._evidence(
                    self._item_write(
                        cursor,
                        event.tool_arguments_delta.item_id,
                        PayloadField.ARGUMENTS,
                        event.tool_arguments_delta.partial_json,
                        append=True,
                    ),
                    entry,
                )
            case "tool_arguments":
                self._evidence(
                    self._complete(
                        cursor,
                        event.tool_arguments.item_id,
                        PayloadField.ARGUMENTS,
                        event.tool_arguments.arguments_json,
                    ),
                    entry,
                )
            case "tool_output_delta":
                self._evidence(
                    self._item_write(
                        cursor,
                        event.tool_output_delta.item_id,
                        PayloadField.OUTPUT,
                        event.tool_output_delta.text,
                        append=True,
                    ),
                    entry,
                )
            case "item_completed":
                completed = event.item_completed
                match completed.WhichOneof("outcome"):
                    case "text":
                        item = self._complete(cursor, completed.item_id, PayloadField.TEXT, completed.text)
                        item = self._save_item(replace(item, completion=TextCompletion()), cursor)
                    case "tool":
                        item = self._complete(cursor, completed.item_id, PayloadField.OUTPUT, completed.tool.output)
                        item = self._save_item(
                            replace(item, completion=ToolCompletion(completed.tool.succeeded)), cursor
                        )
                    case _:
                        raise ObservationNotUnderstoodError(cursor, "item_completed.outcome")
                self._evidence(item, entry)
            case "harness_user_message_confirmed":
                confirmed = event.harness_user_message_confirmed
                owner = PayloadOwner(
                    self.state.position.projection_epoch,
                    cursor,
                    confirmed.harness_message_id or f"confirmed:{cursor}",
                    cursor,
                )
                value = self._write(owner, None, cursor, PayloadField.CONFIRMED_INPUT, confirmed.text, append=False)
                self.confirmed[cursor] = ConfirmedInput(
                    self.state.position.projection_epoch,
                    cursor,
                    cursor,
                    confirmed.harness_message_id,
                    tuple(confirmed.origin_command_ids),
                    confirmed.turn_id or None,
                    value,
                )
                for command_id in confirmed.origin_command_ids:
                    self._settle(entry, command_id, CommandOutcome.EFFECTED)
                self._evidence(cursor, entry)
            case "command_admitted":
                self._admit(cursor, event.command_admitted.command)
                self._evidence(cursor, entry)
            case "command_failed":
                self._settle(entry, event.command_failed.command_id, CommandOutcome.FAILED, event.command_failed.reason)
            case "command_noop":
                self._settle(entry, event.command_noop.command_id, CommandOutcome.NOOP, event.command_noop.reason)
            case kind if kind in _LIFECYCLE:
                self._lifecycle(entry, kind)
                self._evidence(cursor, entry)
            case kind if kind in _SILENT:
                pass
            case _:
                raise ObservationNotUnderstoodError(cursor, observation)


def advance(state: ViewState, batch: EventBatch, prior: PriorEntities) -> ProjectionBatch:
    """Fold a contiguous original-source prefix without mutating caller-owned values."""

    position = state.position
    if not position.source_id or not position.projection_epoch:
        raise ValueError("projection source and epoch are required")
    if batch.source_id != position.source_id:
        raise ValueError("batch source does not match projection")
    if batch.after_cursor != position.through_cursor:
        raise ValueError("batch checkpoint does not match projection")
    fold = _Fold(state, PriorEntities(dict(prior.items), dict(prior.commands), dict(prior.bodies)))
    for supplied in batch.entries:
        entry = event_log_pb2.EventEntry.FromString(supplied.SerializeToString())
        try:
            if entry.cursor != fold.state.position.through_cursor + 1:
                raise ValueError(f"noncontiguous source cursor: {entry.cursor}")
            if entry.origin.source_id != batch.source_id or entry.origin.sequence != entry.cursor:
                raise ValueError("entry does not belong to the original source archive")
            fold.fold(entry)
            fold.state = replace(fold.state, position=replace(fold.state.position, through_cursor=entry.cursor))
        except ObservationNotUnderstoodError:
            raise
        except ValueError as error:
            raise FoldContractError(entry.cursor, str(error)) from error
    return ProjectionBatch(
        fold.state,
        tuple(fold.items.values()),
        tuple(fold.confirmed.values()),
        tuple(fold.lifecycle.values()),
        tuple(fold.commands.values()),
        tuple(fold.evidence),
        tuple(fold.payload_writes.values()),
    )
