"""The pure fold from archived Events to the derived Thread view.

One Segment begins at each conversation-bearing Event, keyed by that Event's immutable cursor, and
later Events revise it in place. `advance` returns the new Projection and the `Changes` batch that
carries a client there, so a client holding a snapshot at `H` and applying the batches through `K`
reaches exactly what a rebuild produces. That equivalence is the contract
<../docs/thread_view_sync.md> calls parity, and `test_projection.py` checks it over randomized batch
boundaries.

The fold emits `thread_view.proto` messages rather than its own types: a Segment's next hop is always
the wire or the read model, so a separate internal representation would only be a copy to keep in
sync. Nothing here touches PostgreSQL, transports or time.

Segments carry whole values inline. Deciding to omit one and leave a `PayloadRef` for an on-demand
read belongs to the layer that serves a response, so every `Text` from this module carries its
`value`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from x.agentplane.app import thread_view_pb2
from x.agentplane.protocol import command_pb2, event_log_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class OperationKind(StrEnum):
    """Which operation a Command carries, for a pending-queue summary that need not open it."""

    SUBMIT_INPUT = "submit_input"
    CHANGE_MODEL = "change_model"
    INTERRUPT_TURN = "interrupt_turn"
    STOP_RUNNER_SESSION = "stop_runner_session"


class UninterpretedObservationError(Exception):
    """An observation this generation of the fold does not model.

    Raised rather than skipped: a projection that silently ignores a newer runner's Event would serve
    a conversation missing whatever that Event said, while reporting full coverage.
    """

    def __init__(self, cursor: int, case: str) -> None:
        super().__init__(f"uninterpreted observation {case!r} at {cursor=}")
        self.cursor = cursor
        self.case = case


@dataclass
class Projection:
    """The complete materialized state of one source at one epoch.

    Unbounded, and never sent whole: `ViewSnapshot` is the bounded, paged view of it.
    """

    position: thread_view_pb2.Position
    # Ordered by anchor cursor: the order the conversation is read in.
    segments: tuple[thread_view_pb2.Segment, ...]
    commands: tuple[thread_view_pb2.CommandSummary, ...]
    controls: thread_view_pb2.Controls


def empty(source_id: str, projection_epoch: str) -> Projection:
    """An empty projection of a known source.

    Both identities are required: a view with no observed source is a projection-not-ready error, not
    an empty view over an invented one.
    """
    if not source_id or not projection_epoch:
        raise ValueError(f"a projection needs both identities: {source_id=} {projection_epoch=}")
    return Projection(
        position=thread_view_pb2.Position(source_id=source_id, projection_epoch=projection_epoch),
        segments=(),
        commands=(),
        controls=thread_view_pb2.Controls(),
    )


def text_of(value: str) -> thread_view_pb2.Text:
    """A value carried inline. Deciding to omit one and leave a reference is the read layer's."""
    return thread_view_pb2.Text(value=value)


def operation_kind(command: command_pb2.Command) -> OperationKind:
    case = command.WhichOneof("operation")
    if case is None:
        raise ValueError(f"Command {command.command_id!r} carries no operation")
    return OperationKind(case)


def _copy_item(message: thread_view_pb2.Item) -> thread_view_pb2.Item:
    clone = thread_view_pb2.Item()
    clone.CopyFrom(message)
    return clone


class _Builder:
    """Mutable working state for one fold, rebuilt from a Projection so `advance` stays a function."""

    def __init__(self, projection: Projection) -> None:
        self.position = thread_view_pb2.Position()
        self.position.CopyFrom(projection.position)
        self.segments = {segment.anchor_cursor: segment for segment in projection.segments}
        self.commands = {summary.command_id: summary for summary in projection.commands}
        self.controls = thread_view_pb2.Controls()
        self.controls.CopyFrom(projection.controls)
        # Anchors let a later Event find the Segment its subject already started.
        self.items = {s.item.item_id: c for c, s in self.segments.items() if s.WhichOneof("content") == "item"}
        self.turns = {s.turn.turn_id: c for c, s in self.segments.items() if s.WhichOneof("content") == "turn"}
        self.touched: dict[int, thread_view_pb2.Segment] = {}
        self.settled: dict[str, thread_view_pb2.CommandSummary] = {}

    def _put(self, anchor: int, cursor: int) -> thread_view_pb2.Segment:
        segment = thread_view_pb2.Segment(anchor_cursor=anchor, revision_cursor=cursor)
        self.segments[anchor] = segment
        self.touched[anchor] = segment
        return segment

    def confirmed_input(self, cursor: int, value: thread_view_pb2.ConfirmedInput) -> None:
        self._put(cursor, cursor).confirmed_input.CopyFrom(value)

    def turn(self, anchor: int, cursor: int, value: thread_view_pb2.Turn) -> None:
        self._put(anchor, cursor).turn.CopyFrom(value)

    def model_effect(self, cursor: int, value: thread_view_pb2.ModelEffect) -> None:
        self._put(cursor, cursor).model_effect.CopyFrom(value)

    def turn_outcome(self, cursor: int, value: thread_view_pb2.TurnOutcome) -> None:
        self._put(cursor, cursor).turn_outcome.CopyFrom(value)

    def lifecycle(self, cursor: int, value: thread_view_pb2.Lifecycle) -> None:
        self._put(cursor, cursor).lifecycle.CopyFrom(value)

    def receipt(self, cursor: int, command_id: str) -> None:
        self._put(cursor, cursor).command_receipt.command_id = command_id

    def item(self, cursor: int, item_id: str) -> tuple[int, thread_view_pb2.Item]:
        """The Item for an id, started here if this Event is the log's first mention of it.

        A delta can precede its `ItemStarted`, so first mention -- not the start Event -- owns the
        anchor; the kind stays unspecified until the start Event names it.
        """
        anchor = self.items.get(item_id)
        if anchor is None:
            self.items[item_id] = cursor
            return cursor, thread_view_pb2.Item(item_id=item_id)
        return anchor, _copy_item(self.segments[anchor].item)

    def put_item(self, anchor: int, cursor: int, value: thread_view_pb2.Item) -> None:
        self._put(anchor, cursor).item.CopyFrom(value)

    def admit(self, cursor: int, command: command_pb2.Command) -> None:
        summary = thread_view_pb2.CommandSummary(
            command_id=command.command_id,
            operation_kind=operation_kind(command),
            admission_cursor=cursor,
            pending=thread_view_pb2.Pending(),
        )
        self.commands[command.command_id] = summary
        self.settled[command.command_id] = summary

    def _settled_copy(self, command_id: str) -> thread_view_pb2.CommandSummary | None:
        """A copy to write a terminal outcome onto, or None when the admission is out of range.

        An outcome can name a Command whose admission precedes this Projection -- an app replica
        replaying from a later checkpoint -- and is then dropped rather than invented: the admission
        Event carries the exact payload, so a summary without it would be a fabrication.
        """
        summary = self.commands.get(command_id)
        if summary is None:
            return None
        settled = thread_view_pb2.CommandSummary()
        settled.CopyFrom(summary)
        return settled

    def _record(self, settled: thread_view_pb2.CommandSummary) -> None:
        self.commands[settled.command_id] = settled
        self.settled[settled.command_id] = settled

    def effected(self, command_id: str, cursor: int) -> None:
        settled = self._settled_copy(command_id)
        if settled is None:
            return
        settled.effected.origin_cursor = cursor
        self._record(settled)

    def failed(self, command_id: str, cursor: int, reason: str) -> None:
        settled = self._settled_copy(command_id)
        if settled is None:
            return
        settled.failed.origin_cursor = cursor
        settled.failed.reason = reason
        self._record(settled)

    def noop(self, command_id: str, cursor: int, reason: str) -> None:
        settled = self._settled_copy(command_id)
        if settled is None:
            return
        settled.noop.origin_cursor = cursor
        settled.noop.reason = reason
        self._record(settled)

    def finish(self) -> Projection:
        return Projection(
            position=self.position,
            segments=tuple(self.segments[cursor] for cursor in sorted(self.segments)),
            commands=tuple(
                self.commands[key] for key in sorted(self.commands, key=lambda k: self.commands[k].admission_cursor)
            ),
            controls=self.controls,
        )


def _fold(builder: _Builder, entry: event_log_pb2.EventEntry) -> None:
    cursor = entry.cursor
    event = entry.event
    case = event.WhichOneof("observation")
    match case:
        case "harness_started":
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_RUNNING
            builder.lifecycle(cursor, thread_view_pb2.Lifecycle(state=thread_view_pb2.HARNESS_STATE_RUNNING))
        case "harness_exited":
            exited = event.harness_exited
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_STOPPED
            builder.lifecycle(
                cursor,
                thread_view_pb2.Lifecycle(
                    state=thread_view_pb2.HARNESS_STATE_STOPPED,
                    exit_code=exited.exit_code,
                    stopped_by_command_id=exited.stopped_by_command_id,
                ),
            )
            if exited.stopped_by_command_id:
                builder.effected(exited.stopped_by_command_id, cursor)
        case "harness_lost":
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_LOST
            builder.lifecycle(cursor, thread_view_pb2.Lifecycle(state=thread_view_pb2.HARNESS_STATE_LOST))
        case "command_admitted":
            command = event.command_admitted.command
            builder.admit(cursor, command)
            builder.receipt(cursor, command.command_id)
        case "command_failed":
            failed = event.command_failed
            builder.failed(failed.command_id, cursor, failed.reason)
            builder.receipt(cursor, failed.command_id)
        case "command_noop":
            noop = event.command_noop
            builder.noop(noop.command_id, cursor, noop.reason)
            builder.receipt(cursor, noop.command_id)
        case "harness_user_message_confirmed":
            confirmed = event.harness_user_message_confirmed
            builder.confirmed_input(
                cursor,
                thread_view_pb2.ConfirmedInput(
                    harness_message_id=confirmed.harness_message_id,
                    text=text_of(confirmed.text),
                    turn_id=confirmed.turn_id,
                    origin_command_ids=confirmed.origin_command_ids,
                ),
            )
            for command_id in confirmed.origin_command_ids:
                builder.effected(command_id, cursor)
        case "turn_started":
            started = event.turn_started
            builder.turns[started.turn_id] = cursor
            builder.turn(cursor, cursor, thread_view_pb2.Turn(turn_id=started.turn_id, model=started.model))
            builder.controls.active_turn_id = started.turn_id
            if started.model:
                builder.controls.applied_model = started.model
        case "turn_completed":
            completed = event.turn_completed
            anchor = builder.turns.get(completed.turn_id)
            if anchor is not None:
                turn = thread_view_pb2.Turn()
                turn.CopyFrom(builder.segments[anchor].turn)
                turn.status = completed.status
                builder.turn(anchor, cursor, turn)
            # The outcome gets its own Segment at the terminal Event, after any partial output.
            builder.turn_outcome(
                cursor,
                thread_view_pb2.TurnOutcome(
                    turn_id=completed.turn_id,
                    status=completed.status,
                    error=completed.error,
                    interrupted_by_command_id=completed.interrupted_by_command_id,
                ),
            )
            if builder.controls.active_turn_id == completed.turn_id:
                builder.controls.ClearField("active_turn_id")
            if completed.interrupted_by_command_id:
                builder.effected(completed.interrupted_by_command_id, cursor)
        case "model_changed":
            changed = event.model_changed
            builder.model_effect(
                cursor,
                thread_view_pb2.ModelEffect(
                    command_id=changed.command_id, previous_model=changed.previous_model, model=changed.model
                ),
            )
            builder.controls.applied_model = changed.model
            if changed.command_id:
                builder.effected(changed.command_id, cursor)
        case "item_started":
            started_item = event.item_started
            anchor, current = builder.item(cursor, started_item.item_id)
            current.kind = started_item.kind
            current.tool_name = started_item.tool_name
            builder.put_item(anchor, cursor, current)
        case "text_delta":
            delta = event.text_delta
            anchor, current = builder.item(cursor, delta.item_id)
            current.text.CopyFrom(text_of(current.text.value + delta.text))
            builder.put_item(anchor, cursor, current)
        case "tool_arguments_delta":
            arguments_delta = event.tool_arguments_delta
            anchor, current = builder.item(cursor, arguments_delta.item_id)
            current.arguments_json.CopyFrom(text_of(current.arguments_json.value + arguments_delta.partial_json))
            builder.put_item(anchor, cursor, current)
        case "tool_arguments":
            arguments = event.tool_arguments
            anchor, current = builder.item(cursor, arguments.item_id)
            current.arguments_json.CopyFrom(text_of(arguments.arguments_json))
            builder.put_item(anchor, cursor, current)
        case "tool_output_delta":
            output_delta = event.tool_output_delta
            anchor, current = builder.item(cursor, output_delta.item_id)
            current.output.CopyFrom(text_of(current.output.value + output_delta.text))
            builder.put_item(anchor, cursor, current)
        case "item_completed":
            completed_item = event.item_completed
            anchor, current = builder.item(cursor, completed_item.item_id)
            outcome = completed_item.WhichOneof("outcome")
            match outcome:
                case "text":
                    current.text.CopyFrom(text_of(completed_item.text))
                    current.completed_text.text.CopyFrom(text_of(completed_item.text))
                case "tool":
                    tool = completed_item.tool
                    current.output.CopyFrom(text_of(tool.output))
                    current.completed_tool.output.CopyFrom(text_of(tool.output))
                    current.completed_tool.succeeded = tool.succeeded
                case _:
                    raise UninterpretedObservationError(cursor, f"item_completed.{outcome}")
            builder.put_item(anchor, cursor, current)
        case "harness_stderr" | "native" | "debug_checkpoint":
            # Carried losslessly in the archive and read on demand. Coverage still advances: an
            # interval of only these Events is a real, fully observed interval.
            pass
        case _:
            raise UninterpretedObservationError(cursor, str(case))


def advance(
    projection: Projection, entries: Iterable[event_log_pb2.EventEntry]
) -> tuple[Projection, thread_view_pb2.Changes]:
    """Fold `entries` onto `projection`, returning it and the batch that carries a client there.

    Entries must be the contiguous archived continuation of the projection's position.
    """
    builder = _Builder(projection)
    after_cursor = projection.position.through_cursor
    for entry in entries:
        if entry.cursor <= builder.position.through_cursor:
            raise ValueError(f"entry {entry.cursor} is not after the projected {builder.position.through_cursor}")
        _fold(builder, entry)
        builder.position.through_cursor = entry.cursor
    advanced = builder.finish()
    return advanced, thread_view_pb2.Changes(
        source_id=advanced.position.source_id,
        projection_epoch=advanced.position.projection_epoch,
        after_cursor=after_cursor,
        through_cursor=advanced.position.through_cursor,
        segments=[builder.touched[cursor] for cursor in sorted(builder.touched)],
        commands=[
            builder.settled[key] for key in sorted(builder.settled, key=lambda k: builder.settled[k].admission_cursor)
        ],
        controls=advanced.controls,
        unresolved_count=sum(1 for summary in advanced.commands if summary.WhichOneof("outcome") == "pending"),
    )


def project(source_id: str, projection_epoch: str, entries: Iterable[event_log_pb2.EventEntry]) -> Projection:
    """The whole fold from empty, for rebuilds and for parity checks against incremental batches."""
    projected, _ = advance(empty(source_id, projection_epoch), entries)
    return projected


def apply_changes(projection: Projection, changes: thread_view_pb2.Changes) -> Projection:
    """What a client does with a change batch: the same result as folding the batch's Events.

    Rejects a batch that does not continue exactly where the client is, or that belongs to another
    source or epoch, rather than guessing across a gap or combining generations.
    """
    position = projection.position
    if changes.after_cursor != position.through_cursor:
        raise ValueError(f"batch after {changes.after_cursor} does not continue {position.through_cursor}")
    if (changes.source_id, changes.projection_epoch) != (position.source_id, position.projection_epoch):
        raise ValueError(
            f"batch is {changes.source_id}/{changes.projection_epoch}, "
            f"view is {position.source_id}/{position.projection_epoch}"
        )
    segments = {segment.anchor_cursor: segment for segment in projection.segments}
    segments.update({segment.anchor_cursor: segment for segment in changes.segments})
    commands = {summary.command_id: summary for summary in projection.commands}
    commands.update({summary.command_id: summary for summary in changes.commands})
    return Projection(
        position=thread_view_pb2.Position(
            source_id=position.source_id,
            projection_epoch=position.projection_epoch,
            through_cursor=changes.through_cursor,
        ),
        segments=tuple(segments[cursor] for cursor in sorted(segments)),
        commands=tuple(sorted(commands.values(), key=lambda summary: summary.admission_cursor)),
        controls=changes.controls,
    )
