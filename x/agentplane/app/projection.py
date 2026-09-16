"""The pure fold from archived Events to the derived Thread view.

One Segment begins at each conversation-bearing Event, keyed by that Event's immutable cursor, and
later Events revise it in place. `advance` returns both the new Projection and the change batch that
carries it there, so a client holding a snapshot at `H` and applying the batches through `K` reaches
exactly `project(entries through K)`. That equivalence is the contract
<../docs/thread_view_sync.md> calls parity, and `test_projection.py` checks it over randomized batch
boundaries.

Nothing here touches PostgreSQL, transports or time: the projector transaction and the RPC layer own
those. The design's `Control` and `GroupBoundary` alternatives are refined into the variants Events
actually produce -- `ModelEffect`/`TurnOutcome` and `Lifecycle`/`CommandReceipt` -- rather than
carrying a discriminator an implementation would have to re-derive. Its `Diagnostic` is not minted:
no observation produces one that `TurnOutcome.error` and `Lifecycle` do not already carry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class HarnessState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    LOST = "lost"


class OperationKind(StrEnum):
    """Which operation a Command carries, for a pending-queue summary that need not open it."""

    SUBMIT_INPUT = "submit_input"
    CHANGE_MODEL = "change_model"
    INTERRUPT_TURN = "interrupt_turn"
    STOP_RUNNER_SESSION = "stop_runner_session"


class UninterpretedObservationError(Exception):
    """An observation this generation of the fold does not model.

    Raised rather than skipped: a projection that silently ignores a newer runner's Event would
    serve a conversation missing whatever that Event said, while reporting full coverage.
    """

    def __init__(self, cursor: int, case: str) -> None:
        super().__init__(f"uninterpreted observation {case!r} at {cursor=}")
        self.cursor = cursor
        self.case = case


@dataclass(frozen=True)
class ConfirmedInput:
    """Harness-confirmed text, anchored at confirmation rather than at submission."""

    harness_message_id: str
    text: str
    turn_id: str | None
    # Every Command that produced this text: Claude coalesces several into one confirmation.
    origin_command_ids: tuple[str, ...]


@dataclass(frozen=True)
class TextOutcome:
    text: str


@dataclass(frozen=True)
class ToolOutcome:
    output: str
    succeeded: bool


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: event_pb2.ItemKind
    tool_name: str
    # What streamed so far. `completion` replaces it with the authoritative value when it arrives.
    text: str
    arguments_json: str
    output: str
    # Absent while streaming, and after a crash that ended the stream: unknown output never becomes
    # a successful completion.
    completion: TextOutcome | ToolOutcome | None


@dataclass(frozen=True)
class Turn:
    turn_id: str
    model: str
    status: event_pb2.TurnStatus | None


@dataclass(frozen=True)
class ModelEffect:
    command_id: str
    previous_model: str
    model: str


@dataclass(frozen=True)
class TurnOutcome:
    """A turn's terminal result, anchored at the terminal Event so it renders after partial output
    rather than again at the turn header."""

    turn_id: str
    status: event_pb2.TurnStatus
    error: str
    interrupted_by_command_id: str


@dataclass(frozen=True)
class Lifecycle:
    state: HarnessState
    exit_code: int
    stopped_by_command_id: str


@dataclass(frozen=True)
class CommandReceipt:
    """An admission or terminal non-effect boundary. It has no normal-mode card, but it is still a
    Segment so that grouping stays deterministic across page edges."""

    command_id: str


SegmentContent = ConfirmedInput | Item | Turn | ModelEffect | TurnOutcome | Lifecycle | CommandReceipt


@dataclass(frozen=True)
class Segment:
    anchor_cursor: int
    revision_cursor: int
    content: SegmentContent


@dataclass(frozen=True)
class Pending:
    pass


@dataclass(frozen=True)
class Effected:
    """The specifically evidenced operation took effect, at this Event."""

    origin_cursor: int


@dataclass(frozen=True)
class Failed:
    origin_cursor: int
    reason: str


@dataclass(frozen=True)
class Noop:
    origin_cursor: int
    reason: str


@dataclass(frozen=True)
class CommandSummary:
    command_id: str
    operation_kind: OperationKind
    admission_cursor: int
    outcome: Pending | Effected | Failed | Noop


@dataclass(frozen=True)
class Controls:
    """Current state as the Events evidence it, never as a request or a local expectation."""

    applied_model: str | None
    active_turn_id: str | None
    harness_state: HarnessState | None


EMPTY_CONTROLS = Controls(applied_model=None, active_turn_id=None, harness_state=None)


@dataclass(frozen=True)
class Projection:
    through_cursor: int
    # Ordered by anchor cursor: the order the conversation is read in.
    segments: tuple[Segment, ...]
    commands: tuple[CommandSummary, ...]
    controls: Controls


EMPTY = Projection(through_cursor=0, segments=(), commands=(), controls=EMPTY_CONTROLS)


@dataclass(frozen=True)
class Batch:
    """Everything that changed over `(after_cursor, through_cursor]`.

    `segments` and `commands` carry complete values rather than patches, so applying a batch never
    depends on the recipient already holding the previous revision. An interval whose Events were all
    native still publishes an empty batch: coverage advanced.
    """

    after_cursor: int
    through_cursor: int
    segments: tuple[Segment, ...]
    commands: tuple[CommandSummary, ...]
    controls: Controls


def operation_kind(command: command_pb2.Command) -> OperationKind:
    case = command.WhichOneof("operation")
    if case is None:
        raise ValueError(f"Command {command.command_id!r} carries no operation")
    return OperationKind(case)


class _Builder:
    """Mutable working state for one fold. Rebuilt from a Projection so `advance` stays pure."""

    def __init__(self, projection: Projection) -> None:
        self.through_cursor = projection.through_cursor
        self.segments = {segment.anchor_cursor: segment for segment in projection.segments}
        self.commands = {summary.command_id: summary for summary in projection.commands}
        self.controls = projection.controls
        # Anchors let a later Event find the Segment its subject already started.
        self.items = {s.content.item_id: c for c, s in self.segments.items() if isinstance(s.content, Item)}
        self.turns = {s.content.turn_id: c for c, s in self.segments.items() if isinstance(s.content, Turn)}
        self.touched: dict[int, Segment] = {}
        self.settled: dict[str, CommandSummary] = {}

    def put(self, cursor: int, content: SegmentContent, *, anchor: int | None = None) -> None:
        """Start a Segment at `cursor`, or revise the one already anchored at `anchor`."""
        at = cursor if anchor is None else anchor
        segment = Segment(anchor_cursor=at, revision_cursor=cursor, content=content)
        self.segments[at] = segment
        self.touched[at] = segment

    def item(self, cursor: int, item_id: str) -> tuple[int, Item]:
        """The Segment for an item, started here if this Event is the log's first mention of it.

        A delta can precede its `ItemStarted`, so first mention -- not the start Event -- owns the
        anchor; the kind stays unspecified until the start Event names it.
        """
        anchor = self.items.get(item_id)
        if anchor is None:
            self.items[item_id] = cursor
            blank = Item(
                item_id=item_id,
                kind=event_pb2.ITEM_KIND_UNSPECIFIED,
                tool_name="",
                text="",
                arguments_json="",
                output="",
                completion=None,
            )
            return cursor, blank
        existing = self.segments[anchor].content
        assert isinstance(existing, Item)
        return anchor, existing

    def settle(self, command_id: str, outcome: Effected | Failed | Noop) -> None:
        """Record a terminal outcome against an admitted Command.

        An outcome can name a Command whose admission is not in this Projection -- an app replica
        replaying from a later checkpoint -- and is then dropped rather than invented: the admission
        Event carries the exact payload, so a summary without it would be a fabrication.
        """
        summary = self.commands.get(command_id)
        if summary is None:
            return
        settled = replace(summary, outcome=outcome)
        self.commands[command_id] = settled
        self.settled[command_id] = settled

    def finish(self) -> Projection:
        return Projection(
            through_cursor=self.through_cursor,
            segments=tuple(self.segments[cursor] for cursor in sorted(self.segments)),
            commands=tuple(self.commands[key] for key in sorted(self.commands, key=self._admission)),
            controls=self.controls,
        )

    def _admission(self, command_id: str) -> int:
        return self.commands[command_id].admission_cursor


def _fold(builder: _Builder, entry: event_log_pb2.EventEntry) -> None:
    cursor = entry.cursor
    event = entry.event
    case = event.WhichOneof("observation")
    match case:
        case "harness_started":
            builder.controls = replace(builder.controls, harness_state=HarnessState.RUNNING)
            builder.put(cursor, Lifecycle(state=HarnessState.RUNNING, exit_code=0, stopped_by_command_id=""))
        case "harness_exited":
            exited = event.harness_exited
            builder.controls = replace(builder.controls, harness_state=HarnessState.STOPPED)
            builder.put(
                cursor,
                Lifecycle(
                    state=HarnessState.STOPPED,
                    exit_code=exited.exit_code,
                    stopped_by_command_id=exited.stopped_by_command_id,
                ),
            )
            if exited.stopped_by_command_id:
                builder.settle(exited.stopped_by_command_id, Effected(origin_cursor=cursor))
        case "harness_lost":
            builder.controls = replace(builder.controls, harness_state=HarnessState.LOST)
            builder.put(cursor, Lifecycle(state=HarnessState.LOST, exit_code=0, stopped_by_command_id=""))
        case "command_admitted":
            command = event.command_admitted.command
            builder.commands[command.command_id] = CommandSummary(
                command_id=command.command_id,
                operation_kind=operation_kind(command),
                admission_cursor=cursor,
                outcome=Pending(),
            )
            builder.settled[command.command_id] = builder.commands[command.command_id]
            builder.put(cursor, CommandReceipt(command_id=command.command_id))
        case "command_failed":
            failed = event.command_failed
            builder.settle(failed.command_id, Failed(origin_cursor=cursor, reason=failed.reason))
            builder.put(cursor, CommandReceipt(command_id=failed.command_id))
        case "command_noop":
            noop = event.command_noop
            builder.settle(noop.command_id, Noop(origin_cursor=cursor, reason=noop.reason))
            builder.put(cursor, CommandReceipt(command_id=noop.command_id))
        case "harness_user_message_confirmed":
            confirmed = event.harness_user_message_confirmed
            builder.put(
                cursor,
                ConfirmedInput(
                    harness_message_id=confirmed.harness_message_id,
                    text=confirmed.text,
                    turn_id=confirmed.turn_id or None,
                    origin_command_ids=tuple(confirmed.origin_command_ids),
                ),
            )
            for command_id in confirmed.origin_command_ids:
                builder.settle(command_id, Effected(origin_cursor=cursor))
        case "turn_started":
            started = event.turn_started
            builder.turns[started.turn_id] = cursor
            builder.put(cursor, Turn(turn_id=started.turn_id, model=started.model, status=None))
            builder.controls = replace(builder.controls, active_turn_id=started.turn_id)
            if started.model:
                builder.controls = replace(builder.controls, applied_model=started.model)
        case "turn_completed":
            completed = event.turn_completed
            anchor = builder.turns.get(completed.turn_id)
            if anchor is not None:
                turn = builder.segments[anchor].content
                assert isinstance(turn, Turn)
                builder.put(cursor, replace(turn, status=completed.status), anchor=anchor)
            # The outcome gets its own Segment at the terminal Event, after any partial output.
            builder.put(
                cursor,
                TurnOutcome(
                    turn_id=completed.turn_id,
                    status=completed.status,
                    error=completed.error,
                    interrupted_by_command_id=completed.interrupted_by_command_id,
                ),
            )
            if builder.controls.active_turn_id == completed.turn_id:
                builder.controls = replace(builder.controls, active_turn_id=None)
            if completed.interrupted_by_command_id:
                builder.settle(completed.interrupted_by_command_id, Effected(origin_cursor=cursor))
        case "model_changed":
            changed = event.model_changed
            builder.put(
                cursor,
                ModelEffect(command_id=changed.command_id, previous_model=changed.previous_model, model=changed.model),
            )
            builder.controls = replace(builder.controls, applied_model=changed.model)
            if changed.command_id:
                builder.settle(changed.command_id, Effected(origin_cursor=cursor))
        case "item_started":
            started_item = event.item_started
            anchor, current = builder.item(cursor, started_item.item_id)
            builder.put(
                cursor, replace(current, kind=started_item.kind, tool_name=started_item.tool_name), anchor=anchor
            )
        case "text_delta":
            delta = event.text_delta
            anchor, current = builder.item(cursor, delta.item_id)
            builder.put(cursor, replace(current, text=current.text + delta.text), anchor=anchor)
        case "tool_arguments_delta":
            arguments_delta = event.tool_arguments_delta
            anchor, current = builder.item(cursor, arguments_delta.item_id)
            builder.put(
                cursor,
                replace(current, arguments_json=current.arguments_json + arguments_delta.partial_json),
                anchor=anchor,
            )
        case "tool_arguments":
            arguments = event.tool_arguments
            anchor, current = builder.item(cursor, arguments.item_id)
            builder.put(cursor, replace(current, arguments_json=arguments.arguments_json), anchor=anchor)
        case "tool_output_delta":
            output_delta = event.tool_output_delta
            anchor, current = builder.item(cursor, output_delta.item_id)
            builder.put(cursor, replace(current, output=current.output + output_delta.text), anchor=anchor)
        case "item_completed":
            completed_item = event.item_completed
            anchor, current = builder.item(cursor, completed_item.item_id)
            outcome = completed_item.WhichOneof("outcome")
            match outcome:
                case "text":
                    finished = replace(
                        current, text=completed_item.text, completion=TextOutcome(text=completed_item.text)
                    )
                case "tool":
                    tool = completed_item.tool
                    finished = replace(
                        current,
                        output=tool.output,
                        completion=ToolOutcome(output=tool.output, succeeded=tool.succeeded),
                    )
                case _:
                    raise UninterpretedObservationError(cursor, f"item_completed.{outcome}")
            builder.put(cursor, finished, anchor=anchor)
        case "harness_stderr" | "native" | "debug_checkpoint":
            # Carried losslessly in the archive and read on demand. Coverage still advances: an
            # interval of only these Events is a real, fully observed interval.
            pass
        case _:
            raise UninterpretedObservationError(cursor, str(case))


def advance(projection: Projection, entries: Iterable[event_log_pb2.EventEntry]) -> tuple[Projection, Batch]:
    """Fold `entries` onto `projection`, returning it and the batch that carries a client there.

    Entries must be the contiguous archived continuation of `projection.through_cursor`.
    """
    builder = _Builder(projection)
    after_cursor = projection.through_cursor
    for entry in entries:
        if entry.cursor <= builder.through_cursor:
            raise ValueError(f"entry {entry.cursor} is not after the projected {builder.through_cursor}")
        _fold(builder, entry)
        builder.through_cursor = entry.cursor
    advanced = builder.finish()
    batch = Batch(
        after_cursor=after_cursor,
        through_cursor=advanced.through_cursor,
        segments=tuple(builder.touched[cursor] for cursor in sorted(builder.touched)),
        commands=tuple(
            builder.settled[key] for key in sorted(builder.settled, key=lambda k: builder.settled[k].admission_cursor)
        ),
        controls=advanced.controls,
    )
    return advanced, batch


def project(entries: Iterable[event_log_pb2.EventEntry]) -> Projection:
    """The whole fold from empty, for rebuilds and for parity checks against incremental batches."""
    projected, _ = advance(EMPTY, entries)
    return projected


def apply_batch(projection: Projection, batch: Batch) -> Projection:
    """What a client does with a change batch: the same result as folding the batch's Events.

    Rejects a batch that does not continue exactly where the client is, rather than guessing across
    a gap or re-applying an overlap.
    """
    if batch.after_cursor != projection.through_cursor:
        raise ValueError(f"batch after {batch.after_cursor} does not continue {projection.through_cursor}")
    segments = {segment.anchor_cursor: segment for segment in projection.segments}
    segments.update({segment.anchor_cursor: segment for segment in batch.segments})
    commands = {summary.command_id: summary for summary in projection.commands}
    commands.update({summary.command_id: summary for summary in batch.commands})
    return Projection(
        through_cursor=batch.through_cursor,
        segments=tuple(segments[cursor] for cursor in sorted(segments)),
        commands=tuple(sorted(commands.values(), key=lambda summary: summary.admission_cursor)),
        controls=batch.controls,
    )
