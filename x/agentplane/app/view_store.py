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
from typing import Any
from uuid import UUID, uuid4

from google.protobuf.json_format import ParseDict
from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, Index, LargeBinary, Text, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from x.agentplane.app import projection, thread_api_pb2, thread_view_pb2
from x.agentplane.app.operator_sessions import Base
from x.agentplane.app.trajectory import Event
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


def _stored(segment: thread_view_pb2.Segment) -> tuple[bytes | None, str | None]:
    """What a Segment persists: an Item's bytes and id, or nothing for a carried Event."""
    if segment.WhichOneof("content") == "item":
        return segment.item.SerializeToString(), segment.item.item_id
    return None, None


class ViewStore:
    """Reads and writes the derived view. One writer at a time per epoch, fenced by a row lock."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @classmethod
    def connect(cls, database_url: str) -> ViewStore:
        return cls(async_sessionmaker(create_async_engine(database_url), expire_on_commit=False))

    async def start(self, thread_id: UUID, source_id: str) -> thread_view_pb2.Position:
        """Create this Thread's first epoch and select it."""
        epoch = uuid4()
        async with self._sessions.begin() as session:
            session.add(
                ProjectionEpoch(
                    thread_id=thread_id,
                    epoch=epoch,
                    source_id=source_id,
                    through_cursor=0,
                    controls=thread_view_pb2.Controls().SerializeToString(),
                )
            )
            await session.flush()
            session.add(ThreadProjection(thread_id=thread_id, selected_epoch=epoch))
        return thread_view_pb2.Position(source_id=source_id, projection_epoch=str(epoch), through_cursor=0)

    async def _selected(self, session: AsyncSession, thread_id: UUID, *, lock: bool = False) -> ProjectionEpoch:
        statement = (
            select(ProjectionEpoch)
            .join(
                ThreadProjection,
                (ThreadProjection.thread_id == ProjectionEpoch.thread_id)
                & (ThreadProjection.selected_epoch == ProjectionEpoch.epoch),
            )
            .where(ProjectionEpoch.thread_id == thread_id)
        )
        # The lock serializes projector transactions without a lease to expire: each batch is atomic
        # and checks the checkpoint it extends, so two workers cannot interleave into a gap.
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise ProjectionNotReadyError(thread_id)
        return row

    def _position(self, row: ProjectionEpoch) -> thread_view_pb2.Position:
        return thread_view_pb2.Position(
            source_id=row.source_id, projection_epoch=str(row.epoch), through_cursor=row.through_cursor
        )

    async def _archived(self, session: AsyncSession, thread_id: UUID, cursors: Sequence[int]) -> dict[int, Any]:
        if not cursors:
            return {}
        rows = await session.execute(
            select(Event.cursor, Event.payload).where(Event.thread_id == thread_id, Event.cursor.in_(cursors))
        )
        return dict(rows.tuples().all())

    async def _rebuild(
        self, session: AsyncSession, thread_id: UUID, rows: Sequence[ViewSegment]
    ) -> list[thread_view_pb2.Segment]:
        """Segments as sent. A row with no Item is the archived Event at its cursor, read back here
        rather than stored a second time."""
        payloads = await self._archived(session, thread_id, [row.cursor for row in rows if row.item is None])
        segments = []
        for row in rows:
            segment = thread_view_pb2.Segment(cursor=row.cursor, revision_cursor=row.revision_cursor)
            if row.item is not None:
                segment.item.ParseFromString(row.item)
            else:
                entry = ParseDict(payloads[row.cursor], event_log_pb2.EventEntry())
                segment.event.CopyFrom(entry.event)
            segments.append(segment)
        return segments

    async def _partial(
        self, session: AsyncSession, row: ProjectionEpoch, items: set[str], commands: set[str]
    ) -> projection.Projection:
        """Only what this batch can touch. Folding a delta must not read the whole conversation."""
        segments: list[thread_view_pb2.Segment] = []
        if items:
            found = await session.scalars(
                select(ViewSegment).where(
                    ViewSegment.thread_id == row.thread_id,
                    ViewSegment.epoch == row.epoch,
                    ViewSegment.item_id.in_(items),
                )
            )
            segments = await self._rebuild(session, row.thread_id, list(found))
        summaries: list[thread_view_pb2.CommandSummary] = []
        if commands:
            found_commands = await session.scalars(
                select(ViewCommand).where(
                    ViewCommand.thread_id == row.thread_id,
                    ViewCommand.epoch == row.epoch,
                    ViewCommand.command_id.in_(commands),
                )
            )
            for command in found_commands:
                summary = thread_view_pb2.CommandSummary()
                summary.ParseFromString(command.summary)
                summaries.append(summary)
        controls = thread_view_pb2.Controls()
        controls.ParseFromString(row.controls)
        return projection.Projection(
            position=self._position(row),
            segments=tuple(sorted(segments, key=lambda segment: segment.cursor)),
            commands=tuple(sorted(summaries, key=lambda summary: summary.admission_cursor)),
            controls=controls,
        )

    async def project(self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> thread_view_pb2.Changes:
        """Fold one batch into the read model and return what to send live followers.

        The whole batch commits or none of it does, so the checkpoint never advances past Segments
        that were not written.
        """
        async with self._sessions.begin() as session:
            row = await self._selected(session, thread_id, lock=True)
            items, commands = _subjects(entries)
            before = await self._partial(session, row, items, commands)
            after, changes = projection.advance(before, entries)

            whole = {segment.cursor: segment for segment in after.segments}
            for cursor in (segment.cursor for segment in changes.segments):
                item, item_id = _stored(whole[cursor])
                await session.execute(
                    insert(ViewSegment)
                    .values(
                        thread_id=thread_id,
                        epoch=row.epoch,
                        cursor=cursor,
                        revision_cursor=whole[cursor].revision_cursor,
                        item=item,
                        item_id=item_id,
                    )
                    .on_conflict_do_update(
                        index_elements=["thread_id", "epoch", "cursor"],
                        set_={"revision_cursor": whole[cursor].revision_cursor, "item": item},
                    )
                )
            for summary in changes.commands:
                await session.execute(
                    insert(ViewCommand)
                    .values(
                        thread_id=thread_id,
                        epoch=row.epoch,
                        command_id=summary.command_id,
                        admission_cursor=summary.admission_cursor,
                        outcome_cursor=_settled_at(summary),
                        summary=summary.SerializeToString(),
                    )
                    .on_conflict_do_update(
                        index_elements=["thread_id", "epoch", "command_id"],
                        set_={"outcome_cursor": _settled_at(summary), "summary": summary.SerializeToString()},
                    )
                )
            row.through_cursor = after.position.through_cursor
            row.controls = after.controls.SerializeToString()
            changes.unresolved_count = await self._unresolved(session, thread_id, row.epoch)
            return changes

    async def _unresolved(self, session: AsyncSession, thread_id: UUID, epoch: UUID) -> int:
        return (
            await session.scalar(
                select(func.count())
                .select_from(ViewCommand)
                .where(
                    ViewCommand.thread_id == thread_id, ViewCommand.epoch == epoch, ViewCommand.outcome_cursor.is_(None)
                )
            )
        ) or 0

    async def _window(
        self, session: AsyncSession, row: ProjectionEpoch, window: thread_api_pb2.SegmentWindow
    ) -> list[ViewSegment]:
        scoped = (ViewSegment.thread_id == row.thread_id, ViewSegment.epoch == row.epoch)
        if not window.HasField("from_cursor"):
            newest = await session.scalars(
                select(ViewSegment).where(*scoped).order_by(ViewSegment.cursor.desc()).limit(window.max_older)
            )
            return sorted(newest, key=lambda found: found.cursor)
        # The Segment at `from_cursor` belongs to the older side, so it asks for one more.
        older = await session.scalars(
            select(ViewSegment)
            .where(*scoped, ViewSegment.cursor <= window.from_cursor)
            .order_by(ViewSegment.cursor.desc())
            .limit(window.max_older + 1)
        )
        newer = await session.scalars(
            select(ViewSegment)
            .where(*scoped, ViewSegment.cursor > window.from_cursor)
            .order_by(ViewSegment.cursor)
            .limit(window.max_newer)
        )
        return sorted([*older, *newer], key=lambda found: found.cursor)

    def _page(self, segments: list[thread_view_pb2.Segment], *, exhausted: bool) -> thread_view_pb2.SegmentsPage:
        return thread_view_pb2.SegmentsPage(
            segments=segments,
            covers_from_cursor=segments[0].cursor if segments else 0,
            covers_through_cursor=segments[-1].cursor if segments else 0,
            exhausted=exhausted,
        )

    async def _pending(
        self, session: AsyncSession, row: ProjectionEpoch, *, after: int = 0, limit: int = 50
    ) -> thread_view_pb2.CommandsPage:
        found = await session.scalars(
            select(ViewCommand)
            .where(
                ViewCommand.thread_id == row.thread_id,
                ViewCommand.epoch == row.epoch,
                ViewCommand.outcome_cursor.is_(None),
                ViewCommand.admission_cursor > after,
            )
            .order_by(ViewCommand.admission_cursor)
            .limit(limit + 1)
        )
        commands = list(found)
        summaries = []
        for command in commands[:limit]:
            summary = thread_view_pb2.CommandSummary()
            summary.ParseFromString(command.summary)
            summaries.append(summary)
        return thread_view_pb2.CommandsPage(
            commands=summaries,
            exhausted=len(commands) <= limit,
            unresolved_count=await self._unresolved(session, row.thread_id, row.epoch),
        )

    async def snapshot(self, thread_id: UUID, window: thread_api_pb2.SegmentWindow) -> thread_view_pb2.ViewSnapshot:
        """Everything a caller needs to render and to start following, read in one transaction so the
        Position it returns is what every other part of the answer is consistent with."""
        async with self._sessions.begin() as session:
            row = await self._selected(session, thread_id)
            rows = await self._window(session, row, window)
            segments = await self._rebuild(session, thread_id, rows)
            oldest = await session.scalar(
                select(func.min(ViewSegment.cursor)).where(
                    ViewSegment.thread_id == thread_id, ViewSegment.epoch == row.epoch
                )
            )
            controls = thread_view_pb2.Controls()
            controls.ParseFromString(row.controls)
            return thread_view_pb2.ViewSnapshot(
                position=self._position(row),
                window=self._page(segments, exhausted=not segments or segments[0].cursor == oldest),
                controls=controls,
                pending=await self._pending(session, row),
            )

    async def catch_up(self, thread_id: UUID, position: thread_view_pb2.Position) -> thread_view_pb2.ViewUpdate:
        """What changed since a caller's Position, read from the live table rather than a journal.

        Segments come back whole: a caller that was away does not hold the revisions extensions would
        apply to, and a range scan on `revision_cursor` is what a stored journal would have replayed.
        """
        async with self._sessions.begin() as session:
            row = await self._selected(session, thread_id)
            if str(row.epoch) != position.projection_epoch:
                return thread_view_pb2.ViewUpdate(
                    rebootstrap_required=thread_view_pb2.RebootstrapRequired(
                        reason=thread_view_pb2.RebootstrapRequired.REASON_PROJECTION_REBUILT
                    )
                )
            if row.source_id != position.source_id:
                raise ValueError(f"source is {row.source_id!r}, caller followed {position.source_id!r}")
            changed = await session.scalars(
                select(ViewSegment)
                .where(
                    ViewSegment.thread_id == thread_id,
                    ViewSegment.epoch == row.epoch,
                    ViewSegment.revision_cursor > position.through_cursor,
                )
                .order_by(ViewSegment.cursor)
            )
            settled = await session.scalars(
                select(ViewCommand)
                .where(
                    ViewCommand.thread_id == thread_id,
                    ViewCommand.epoch == row.epoch,
                    func.greatest(ViewCommand.admission_cursor, func.coalesce(ViewCommand.outcome_cursor, 0))
                    > position.through_cursor,
                )
                .order_by(ViewCommand.admission_cursor)
            )
            summaries = []
            for command in settled:
                summary = thread_view_pb2.CommandSummary()
                summary.ParseFromString(command.summary)
                summaries.append(summary)
            controls = thread_view_pb2.Controls()
            controls.ParseFromString(row.controls)
            return thread_view_pb2.ViewUpdate(
                changes=thread_view_pb2.Changes(
                    source_id=row.source_id,
                    projection_epoch=str(row.epoch),
                    after_cursor=position.through_cursor,
                    through_cursor=row.through_cursor,
                    segments=await self._rebuild(session, thread_id, list(changed)),
                    commands=summaries,
                    controls=controls,
                    unresolved_count=await self._unresolved(session, thread_id, row.epoch),
                )
            )
