"""The pure fold from archived Events to the derived Thread view.

The projection does three things, and deliberately not a fourth: it accumulates the deltas that make
up an Item, it drops Events a reader never sees, and it keys what remains by the cursor it arrived
at. An Event whose meaning is already its final state is carried verbatim, so the view has one
vocabulary -- `protocol/event.proto` -- rather than a parallel transcription of it.

What is genuinely derived, because each joins or accumulates across Events rather than renaming one:

- `Item`, which accumulates text, argument and output deltas.
- `CommandSummary`, which joins an admission at one cursor with its outcome at a later one.
- `Controls`, latest-wins over the prefix.

`advance` returns the new Projection and the `Changes` batch that carries a client there, so a client
holding a snapshot at `H` and applying the batches through `K` reaches exactly what a rebuild
produces. That equivalence is what <../docs/thread_view_sync.md> calls parity, and
`test_projection.py` checks it over randomized batch boundaries.

Nothing here touches PostgreSQL, transports or time. Segments carry whole values inline; deciding to
omit one and leave a `PayloadRef` belongs to the layer that serves a response. Grouping runs of tool
calls and reasoning is a rendering rule over whatever the client is showing, not a fact about the
log, so no Segment exists to mark one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from x.agentplane.app import thread_view_pb2
from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# Events carried into the conversation as they are. Their meaning is already their final state, so
# transcribing them into parallel messages would add a vocabulary without adding a fact.
CARRIED = frozenset(
    {
        "harness_user_message_confirmed",
        "turn_started",
        "turn_completed",
        "model_changed",
        "harness_started",
        "harness_exited",
        "harness_lost",
    }
)

# Observed, covered by the checkpoint, and deliberately not part of the conversation. Native frames
# and stderr are read on demand through ThreadEventsService; Command Events become CommandSummary.
SILENT = frozenset({"native", "debug_checkpoint", "harness_stderr"})

ITEM_EVENTS = frozenset(
    {"item_started", "text_delta", "tool_arguments_delta", "tool_arguments", "tool_output_delta", "item_completed"}
)


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
    # Ordered by started_at cursor: the order the conversation is read in.
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


class _Builder:
    """Mutable working state for one fold, rebuilt from a Projection so `advance` stays a function."""

    def __init__(self, projection: Projection) -> None:
        self.position = thread_view_pb2.Position()
        self.position.CopyFrom(projection.position)
        self.segments = {segment.cursor: segment for segment in projection.segments}
        self.commands = {summary.command_id: summary for summary in projection.commands}
        self.controls = thread_view_pb2.Controls()
        self.controls.CopyFrom(projection.controls)
        # Lets a later delta find the Segment its Item already started at.
        self.items = {s.item.item_id: c for c, s in self.segments.items() if s.WhichOneof("content") == "item"}
        self.touched: dict[int, thread_view_pb2.Segment] = {}
        self.settled: dict[str, thread_view_pb2.CommandSummary] = {}

    def carry(self, cursor: int, event: event_pb2.Event) -> None:
        segment = thread_view_pb2.Segment(cursor=cursor, revision_cursor=cursor)
        segment.event.CopyFrom(event)
        self.segments[cursor] = segment
        self.touched[cursor] = segment

    def item(self, cursor: int, item_id: str) -> tuple[int, thread_view_pb2.Item]:
        """The Item for an id, started here if this Event is the log's first mention of it.

        A delta can precede its `ItemStarted`, so first mention -- not the start Event -- owns the
        Segment's cursor; the kind stays unspecified until the start Event names it.
        """
        started_at = self.items.get(item_id)
        if started_at is None:
            self.items[item_id] = cursor
            return cursor, thread_view_pb2.Item(item_id=item_id)
        clone = thread_view_pb2.Item()
        clone.CopyFrom(self.segments[started_at].item)
        return started_at, clone

    def put_item(self, started_at: int, cursor: int, value: thread_view_pb2.Item) -> None:
        segment = thread_view_pb2.Segment(cursor=started_at, revision_cursor=cursor)
        segment.item.CopyFrom(value)
        self.segments[started_at] = segment
        self.touched[started_at] = segment

    def admit(self, cursor: int, command: command_pb2.Command) -> None:
        summary = thread_view_pb2.CommandSummary(
            command_id=command.command_id,
            operation_kind=operation_kind(command),
            admission_cursor=cursor,
            pending=thread_view_pb2.Pending(),
        )
        self.commands[command.command_id] = summary
        self.settled[command.command_id] = summary

    def _settling(self, command_id: str) -> thread_view_pb2.CommandSummary | None:
        """A copy to write a terminal outcome onto, or None when the admission is out of range.

        An outcome can name a Command whose admission precedes this Projection -- an app replica
        replaying from a later checkpoint -- and is then dropped rather than invented: the admission
        Event carries the exact payload, so a summary without it would be a fabrication.
        """
        summary = self.commands.get(command_id)
        if summary is None:
            return None
        settling = thread_view_pb2.CommandSummary()
        settling.CopyFrom(summary)
        return settling

    def _record(self, settled: thread_view_pb2.CommandSummary) -> None:
        self.commands[settled.command_id] = settled
        self.settled[settled.command_id] = settled

    def effected(self, command_id: str, cursor: int) -> None:
        settling = self._settling(command_id)
        if settling is None:
            return
        settling.effected.origin_cursor = cursor
        self._record(settling)

    def failed(self, command_id: str, cursor: int, reason: str) -> None:
        settling = self._settling(command_id)
        if settling is None:
            return
        settling.failed.origin_cursor = cursor
        settling.failed.reason = reason
        self._record(settling)

    def noop(self, command_id: str, cursor: int, reason: str) -> None:
        settling = self._settling(command_id)
        if settling is None:
            return
        settling.noop.origin_cursor = cursor
        settling.noop.reason = reason
        self._record(settling)

    def finish(self) -> Projection:
        return Projection(
            position=self.position,
            segments=tuple(self.segments[cursor] for cursor in sorted(self.segments)),
            commands=tuple(
                self.commands[key] for key in sorted(self.commands, key=lambda k: self.commands[k].admission_cursor)
            ),
            controls=self.controls,
        )


def _fold_item(builder: _Builder, cursor: int, case: str, event: event_pb2.Event) -> None:
    match case:
        case "item_started":
            started = event.item_started
            started_at, item = builder.item(cursor, started.item_id)
            item.kind = started.kind
            item.tool_name = started.tool_name
        case "text_delta":
            delta = event.text_delta
            started_at, item = builder.item(cursor, delta.item_id)
            item.text.CopyFrom(text_of(item.text.value + delta.text))
        case "tool_arguments_delta":
            arguments_delta = event.tool_arguments_delta
            started_at, item = builder.item(cursor, arguments_delta.item_id)
            item.arguments_json.CopyFrom(text_of(item.arguments_json.value + arguments_delta.partial_json))
        case "tool_arguments":
            arguments = event.tool_arguments
            started_at, item = builder.item(cursor, arguments.item_id)
            item.arguments_json.CopyFrom(text_of(arguments.arguments_json))
        case "tool_output_delta":
            output_delta = event.tool_output_delta
            started_at, item = builder.item(cursor, output_delta.item_id)
            item.output.CopyFrom(text_of(item.output.value + output_delta.text))
        case "item_completed":
            completed = event.item_completed
            started_at, item = builder.item(cursor, completed.item_id)
            outcome = completed.WhichOneof("outcome")
            match outcome:
                case "text":
                    item.text.CopyFrom(text_of(completed.text))
                    item.completed_text.SetInParent()
                case "tool":
                    item.output.CopyFrom(text_of(completed.tool.output))
                    item.completed_tool.succeeded = completed.tool.succeeded
                case _:
                    raise UninterpretedObservationError(cursor, f"item_completed.{outcome}")
        case _:
            raise UninterpretedObservationError(cursor, case)
    builder.put_item(started_at, cursor, item)


def _fold(builder: _Builder, entry: event_log_pb2.EventEntry) -> None:
    cursor = entry.cursor
    event = entry.event
    case = event.WhichOneof("observation")

    if case in ITEM_EVENTS:
        _fold_item(builder, cursor, case, event)
        return
    if case in SILENT:
        return

    # Command Events drive the queue rather than the conversation, so they settle summaries and
    # controls without producing a Segment.
    match case:
        case "command_admitted":
            builder.admit(cursor, event.command_admitted.command)
            return
        case "command_failed":
            builder.failed(event.command_failed.command_id, cursor, event.command_failed.reason)
            return
        case "command_noop":
            builder.noop(event.command_noop.command_id, cursor, event.command_noop.reason)
            return

    if case not in CARRIED:
        raise UninterpretedObservationError(cursor, str(case))

    builder.carry(cursor, event)
    match case:
        case "harness_user_message_confirmed":
            for command_id in event.harness_user_message_confirmed.origin_command_ids:
                builder.effected(command_id, cursor)
        case "turn_started":
            started = event.turn_started
            builder.controls.active_turn_id = started.turn_id
            if started.model:
                builder.controls.applied_model = started.model
        case "turn_completed":
            completed = event.turn_completed
            if builder.controls.active_turn_id == completed.turn_id:
                builder.controls.ClearField("active_turn_id")
            if completed.interrupted_by_command_id:
                builder.effected(completed.interrupted_by_command_id, cursor)
        case "model_changed":
            changed = event.model_changed
            builder.controls.applied_model = changed.model
            if changed.command_id:
                builder.effected(changed.command_id, cursor)
        case "harness_started":
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_RUNNING
        case "harness_exited":
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_STOPPED
            if event.harness_exited.stopped_by_command_id:
                builder.effected(event.harness_exited.stopped_by_command_id, cursor)
        case "harness_lost":
            builder.controls.harness_state = thread_view_pb2.HARNESS_STATE_LOST


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
    segments = {segment.cursor: segment for segment in projection.segments}
    segments.update({segment.cursor: segment for segment in changes.segments})
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
