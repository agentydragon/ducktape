"""Bounded service-history projection under app replica leases.

Resume the UI checkpoint without acquiring runner storage or copying raw Event rows.
"""

import logging
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

import grpc
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events import projection_lease
from agentplane.app.threads.events.event_log import EventReplicationError
from agentplane.app.threads.events.projection_lease import ProjectionLease, ProjectionLeaseLostError
from agentplane.app.threads.model_activity import record_model_activity
from agentplane.app.threads.models import ThreadCheckpoint, ThreadHistorySummary
from agentplane.app.threads.projected_lifecycle import project_lifecycle
from agentplane.app.threads.view.recording import record_thread_fold, set_operational
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service:protocol_pb2


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionProgress:
    through_cursor: int
    service_cursor: int


class HistoryProjector:
    def __init__(self, engine: AsyncEngine, reader: SandboxServiceClient) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._reader = reader

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
            await notify(session, Channel.THREADS)

    async def _project_prefix(self, thread_id: UUID, *, lease: ProjectionLease, after: int) -> ProjectionProgress:
        page = await self._reader.read_session_events(str(thread_id), after_cursor=after, limit=128)
        if page.last_cursor < after:
            raise ConnectionError("Sandbox Service has not covered the app projection checkpoint")
        if len(page.entries) > 128 or any(
            entry.cursor != after + index + 1
            or entry.cursor > page.last_cursor
            or entry.origin.sequence != entry.cursor
            or not entry.origin.source_id
            for index, entry in enumerate(page.entries)
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
                    await notify(session, Channel.THREADS)
                return ProjectionProgress(after, page.last_cursor)
            source_id = page.entries[0].origin.source_id
            if any(entry.origin.source_id != source_id for entry in page.entries):
                raise EventReplicationError("mixed source identities in projection page")
            await record_thread_fold(session, thread_id, source_id, page.entries)
            await record_model_activity(session, thread_id, page.entries)
            for entry in page.entries:
                at = entry.event.at.ToDatetime(tzinfo=UTC)
                if summary.last_event_at is None or at > summary.last_event_at:
                    summary.last_event_at = at
                if entry.event.HasField("turn_completed"):
                    summary.last_turn_status = entry.event.turn_completed.status

            await project_lifecycle(session, thread_id, summary, page, through_cursor=page.entries[-1].cursor)
            await notify(session, Channel.THREADS)
        return ProjectionProgress(page.entries[-1].cursor, page.last_cursor)
