"""Bounded service-history projection under per-Session replica leases.

Resume the UI checkpoint without acquiring runner storage or copying raw Event rows.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

import grpc
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.sandbox_service.settled_cursors import next_stored, settled_ranges
from agentplane.thread_fold import projection_lease
from agentplane.thread_fold.lifecycle import project_lifecycle
from agentplane.thread_fold.model_activity import record_model_activity
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadHistorySummary
from agentplane.thread_fold.projection_lease import ProjectionLease, ProjectionLeaseLostError
from agentplane.thread_fold.recording import EventReplicationError, record_thread_fold, set_operational

# gazelle:include_dep @pypi//protobuf


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionProgress:
    through_cursor: int
    service_cursor: int


# Up to `limit` entries of a Session's committed prefix after a cursor, with its watermark and lifecycle.
PageReader = Callable[[UUID, int, int], Awaitable[history_pb2.ReadSessionEventsResponse]]


class HistoryProjector:
    def __init__(
        self,
        engine: AsyncEngine,
        read_page: PageReader,
        *,
        projection_epoch: str,
        page_size: int,
        notify: Callable[[AsyncSession], Awaitable[None]],
    ) -> None:
        """Fold at most `page_size` entries per transaction; `notify` runs inside each transaction
        that changes what a Thread's readers see."""
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._read_page = read_page
        self._page_size = page_size
        self._projection_epoch = projection_epoch
        self._notify = notify

    async def project_batch(self, thread_id: UUID, *, lease: ProjectionLease) -> ProjectionProgress:
        """Resume from the UI checkpoint; a failed fold leaves that checkpoint unchanged."""
        async with self._sessions() as session:
            after = (
                await session.scalar(
                    select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread_id)
                )
                or 0
            )
        try:
            return await self._project_prefix(thread_id, lease=lease, after=after)
        except (ConnectionError, TimeoutError, grpc.RpcError, ValueError) as error:
            try:
                await self._record_failure(thread_id, lease=lease, after=after, error=error)
            except ProjectionLeaseLostError, SQLAlchemyError:
                logger.warning("could not retain projection failure for %s", thread_id, exc_info=True)
            raise

    async def _record_failure(self, thread_id: UUID, *, lease: ProjectionLease, after: int, error: Exception) -> None:
        async with self._sessions.begin() as session:
            await projection_lease.fence(session, lease, thread_id)
            checkpoint = await session.get(ThreadCheckpoint, thread_id, with_for_update=True)
            if checkpoint is None or checkpoint.through_cursor != after:
                return  # no materialized view, or a newer batch already recovered
            # Caller-visible diagnostics never include backend or raw event text.
            await set_operational(
                session,
                thread_id,
                status="failed",
                error="Session history projection stalled; retained events and the last verified view are unchanged.",
                error_cursor=error.cursor if isinstance(error, EventReplicationError) else None,
            )
            await self._notify(session)

    async def _project_prefix(self, thread_id: UUID, *, lease: ProjectionLease, after: int) -> ProjectionProgress:
        page = await self._read_page(thread_id, after, self._page_size)
        if page.last_cursor < after:
            raise ConnectionError("History Service has not covered the app projection checkpoint")
        if len(page.entries) > self._page_size:
            raise EventReplicationError("invalid service history projection page")
        # Cursors a settlement removed are covered by its ranges, not missing.
        settled = settled_ranges(cursor_range for settlement in page.settlements for cursor_range in settlement.ranges)
        expected = after
        for entry in page.entries:
            expected = next_stored(expected, settled)
            if (
                entry.cursor != expected
                or entry.cursor > page.last_cursor
                or entry.origin.sequence != entry.cursor
                or not entry.origin.source_id
            ):
                raise EventReplicationError("invalid service history projection page")
        if not page.entries and after < page.last_cursor:
            raise EventReplicationError("service omitted entries from its committed prefix")
        async with self._sessions.begin() as session:
            # Serializes with other writes under this sandbox lease, including a
            # delayed batch from an old owner. Do not hold the fence over the RPC.
            await projection_lease.fence(session, lease, thread_id)
            current = (
                await session.scalar(
                    select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread_id)
                )
                or 0
            )
            if current != after:
                raise ConnectionError("projection checkpoint advanced during fetch; retry from checkpoint")
            summary = await session.get(ThreadHistorySummary, thread_id)
            if summary is None:
                raise EventReplicationError("missing service projection metadata")
            if not page.entries:
                if await project_lifecycle(session, thread_id, summary, page, through_cursor=after):
                    await self._notify(session)
                return ProjectionProgress(after, page.last_cursor)
            source_id = page.entries[0].origin.source_id
            if any(entry.origin.source_id != source_id for entry in page.entries):
                raise EventReplicationError("mixed source identities in projection page")
            await record_thread_fold(
                session,
                thread_id,
                source_id,
                page.entries,
                settlements=page.settlements,
                projection_epoch=self._projection_epoch,
            )
            await record_model_activity(session, thread_id, page.entries)
            for entry in page.entries:
                at = entry.event.at.ToDatetime(tzinfo=UTC)
                if entry.cursor == 1:
                    summary.first_event_at = at
                if summary.last_event_at is None or at > summary.last_event_at:
                    summary.last_event_at = at
                if entry.event.HasField("turn_completed"):
                    summary.last_turn_status = entry.event.turn_completed.status

            await project_lifecycle(session, thread_id, summary, page, through_cursor=page.entries[-1].cursor)
            await self._notify(session)
        return ProjectionProgress(page.entries[-1].cursor, page.last_cursor)
