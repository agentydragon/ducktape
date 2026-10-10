"""The thread fold over the history database, built from its raw log beside the app's projection.

Every retained Session is folded from cursor 0 in its own epoch, so starting it is the rebuild: each
cycle advances every Session by at most one page, at most `concurrency` at a time, under the same
per-Session leases the app projector uses, so replicas share the work and live Sessions keep up.
A Session's progress is its `thread_checkpoint` row.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service.summary_changes import notify_summaries
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.db import SessionHistory
from agentplane.sandbox_service.session_history.store import Store
from agentplane.thread_fold import projection_lease
from agentplane.thread_fold.models import ThreadHistorySummary
from agentplane.thread_fold.projection_lease import ProjectionLease, ProjectionLeaseLostError
from agentplane.thread_fold.projector import HistoryProjector, PageReader, ProjectionProgress
from agentplane.thread_fold.recording import EventReplicationError

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Distinct from the app's, so a reader holding rows of one projection sees a scope reset on the other.
FOLD_EPOCH = "history-v1"
LEASE_DURATION = timedelta(seconds=30)
PAGE_SIZE = 128


def history_pages(history: Store) -> PageReader:
    async def read(session_id: UUID, after_cursor: int, limit: int) -> protocol_pb2.ReadSessionEventsResponse:
        return await history.read_page(session_id, after_cursor=after_cursor, limit=limit)

    return read


@dataclass(frozen=True)
class FoldBacklog:
    sessions: int
    behind: int
    events_behind: int


class FoldShadow:
    def __init__(self, *, engine: AsyncEngine, history: Store, concurrency: int) -> None:
        """`engine` writes the fold tables; `history` reads the raw log, which this never writes."""
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._projector = HistoryProjector(
            engine, history_pages(history), projection_epoch=FOLD_EPOCH, notify=notify_summaries
        )
        self._slots = asyncio.Semaphore(concurrency)
        self._leases: dict[UUID, ProjectionLease] = {}

    async def run(self, *, interval_s: float) -> None:
        try:
            while True:
                try:
                    backlog = await self.cycle()
                    logger.info(
                        "fold shadow: %d of %d Sessions behind, %d events behind",
                        backlog.behind,
                        backlog.sessions,
                        backlog.events_behind,
                    )
                except SQLAlchemyError, OSError:
                    logger.warning("fold shadow cycle failed; will retry", exc_info=True)
                await asyncio.sleep(interval_s)
        finally:
            for session_id in list(self._leases):
                await self._release(session_id)

    async def cycle(self) -> FoldBacklog:
        """Advance every retained Session by at most one page."""
        session_ids = await self._discover()
        for session_id in set(self._leases) - session_ids:
            await self._release(session_id)
        async with asyncio.TaskGroup() as tasks:
            advancing = [tasks.create_task(self._advance(session_id)) for session_id in session_ids]
        progress = [task.result() for task in advancing]
        behind = [p for p in progress if p is not None and p.through_cursor < p.service_cursor]
        return FoldBacklog(
            sessions=len(session_ids),
            behind=len(behind),
            events_behind=sum(p.service_cursor - p.through_cursor for p in behind),
        )

    async def _discover(self) -> set[UUID]:
        """Every retained Session, including those of deleted Sandboxes, with a summary row to fold into."""
        async with self._sessions.begin() as session:
            session_ids = set(await session.scalars(select(SessionHistory.id)))
            if session_ids:
                await session.execute(
                    insert(ThreadHistorySummary)
                    .values([{"thread_id": session_id} for session_id in session_ids])
                    .on_conflict_do_nothing()
                )
        return session_ids

    async def _advance(self, session_id: UUID) -> ProjectionProgress | None:
        async with self._slots:
            try:
                lease = await self._lease(session_id)
                if lease is None:
                    return None  # another replica folds this Session
                progress = await self._projector.project_batch(session_id, lease=lease)
            except (
                EventReplicationError,
                ProjectionLeaseLostError,
                SQLAlchemyError,
                LookupError,
                ConnectionError,
                ValueError,
                OSError,
            ):
                # One Session's failed fold must not stop the others; its checkpoint is retained.
                logger.warning("fold shadow stalled for %s", session_id, exc_info=True)
                return None
            logger.debug(
                "fold shadow %s: through=%s committed=%s", session_id, progress.through_cursor, progress.service_cursor
            )
            return progress

    async def _lease(self, session_id: UUID) -> ProjectionLease | None:
        async with self._sessions.begin() as session:
            lease = self._leases.get(session_id)
            if lease is not None and await projection_lease.renew(session, lease, LEASE_DURATION):
                return lease
            self._leases.pop(session_id, None)
            acquired = await projection_lease.acquire(session, session_id, LEASE_DURATION)
        if acquired is not None:
            self._leases[session_id] = acquired
        return acquired

    async def _release(self, session_id: UUID) -> None:
        async with self._sessions.begin() as session:
            await projection_lease.release(session, self._leases.pop(session_id))
