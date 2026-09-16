"""The derived Thread view, materialized in PostgreSQL.

Everything here is rebuildable from the `event` archive, which stays the only source of truth, so the
representation stores each fact once wherever it can:

- A Segment whose content is a carried Event stores no content. The archive already holds that Event
  at the same cursor, which is also the Segment's identity, so the row is a cursor and a revision.
- There is no journal of derived updates. Catching a reconnected caller up is a range scan on
  `revision_cursor`, which answers "what changed since H" from the live table.

Live followers receive extensions, which the projector computes as it commits and never stores: only
a caller currently connected holds the revision an extension applies to. A reconnecting one gets the
changed Segments whole, costing one Segment's size rather than a retained journal.

Contract: <../docs/thread_view_sync.md>. Fold: `projection.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, Index, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from x.agentplane.app import thread_view_pb2
from x.agentplane.app.operator_sessions import Base
from x.agentplane.protocol import event_log_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class ProjectionEpoch(Base):
    __tablename__ = "projection_epoch"

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text)
    through_cursor: Mapped[int] = mapped_column(BigInteger)
    controls: Mapped[bytes] = mapped_column(LargeBinary)


class ThreadProjection(Base):
    """Which generation reads see. Selecting a rebuilt epoch is one UPDATE, and so atomic."""

    __tablename__ = "thread_projection"
    __table_args__ = (
        ForeignKeyConstraint(["thread_id", "selected_epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"]),
    )

    thread_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
    )
    selected_epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))


class ViewSegment(Base):
    __tablename__ = "view_segment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"], ondelete="CASCADE"
        ),
        Index("view_segment_revision", "thread_id", "epoch", "revision_cursor"),
        Index(
            "view_segment_item",
            "thread_id",
            "epoch",
            "item_id",
            unique=True,
            postgresql_where=text("item_id IS NOT NULL"),
        ),
    )

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_cursor: Mapped[int] = mapped_column(BigInteger)
    # NULL means this Segment is the archived Event at `cursor`; only a folded Item is stored.
    item: Mapped[bytes | None] = mapped_column(LargeBinary)
    # The Item's id, so a later delta finds the Segment its Item started at without scanning. A key
    # extracted from the blob, the way `cursor` and `revision_cursor` are.
    item_id: Mapped[str | None] = mapped_column(Text)


class ViewCommand(Base):
    __tablename__ = "view_command"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"], ondelete="CASCADE"
        ),
        Index("view_command_admission", "thread_id", "epoch", "admission_cursor"),
        Index(
            "view_command_pending",
            "thread_id",
            "epoch",
            "admission_cursor",
            postgresql_where=text("outcome_cursor IS NULL"),
        ),
    )

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    admission_cursor: Mapped[int] = mapped_column(BigInteger)
    # NULL while pending. The cursor that settled it is a fact; a status column would cache this and
    # could then disagree with the summary beside it.
    outcome_cursor: Mapped[int | None] = mapped_column(BigInteger)
    summary: Mapped[bytes] = mapped_column(LargeBinary)


class ProjectionNotReadyError(Exception):
    """No epoch has observed this Thread's source yet, which is not an empty view over an invented one."""

    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no projection for thread {thread_id}")
        self.thread_id = thread_id


def _settled_at(summary: thread_view_pb2.CommandSummary) -> int | None:
    match summary.WhichOneof("outcome"):
        case "effected":
            return summary.effected.origin_cursor
        case "failed":
            return summary.failed.origin_cursor
        case "noop":
            return summary.noop.origin_cursor
        case _:
            return None


def _subjects(entries: Sequence[event_log_pb2.EventEntry]) -> tuple[set[str], set[str]]:
    """The item and command ids a batch touches, so folding it loads those and nothing else.

    Without this the projector would read the whole conversation to append one delta to it, which is
    the cost the derived view exists to avoid.
    """
    items: set[str] = set()
    commands: set[str] = set()
    for entry in entries:
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
            case "tool_output_delta":
                items.add(event.tool_output_delta.item_id)
            case "item_completed":
                items.add(event.item_completed.item_id)
            case "command_admitted":
                commands.add(event.command_admitted.command.command_id)
            case "command_failed":
                commands.add(event.command_failed.command_id)
            case "command_noop":
                commands.add(event.command_noop.command_id)
            case "harness_user_message_confirmed":
                commands.update(event.harness_user_message_confirmed.origin_command_ids)
            case "turn_completed":
                if event.turn_completed.interrupted_by_command_id:
                    commands.add(event.turn_completed.interrupted_by_command_id)
            case "model_changed":
                if event.model_changed.command_id:
                    commands.add(event.model_changed.command_id)
            case "harness_exited":
                if event.harness_exited.stopped_by_command_id:
                    commands.add(event.harness_exited.stopped_by_command_id)
    return items, commands
