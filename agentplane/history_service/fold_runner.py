"""Runs the Thread fold over the history database, built from its raw log.

Every retained Session is folded from cursor 0 in its own epoch, so starting it is the rebuild: each
cycle advances every Session by at most one page, at most `concurrency` at a time, under per-Session
leases, so replicas share the work and live Sessions keep up. A Session's progress is its
`thread_checkpoint` row.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from asyncpg import PostgresError
from grpc_health.v1 import health, health_pb2
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service.fold import projection_lease
from agentplane.history_service.fold.models import ThreadHistorySummary
from agentplane.history_service.fold.projection_lease import ProjectionLease, ProjectionLeaseLostError
from agentplane.history_service.fold.projector import HistoryProjector, ProjectionProgress
from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.storage.history_changes import HistoryChanges
from agentplane.history_service.storage.store import Store

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Every fold row carries it, so a reader holding rows of another epoch sees a scope reset.
FOLD_EPOCH = "history-v1"
LEASE_DURATION = timedelta(seconds=30)
# The `grpc.health.v1` service whose status says whether this replica's fold is running. Probes
# check the whole server (`""`), so a stopped fold leaves raw reads served.
HEALTH_SERVICE = "agentplane.history_service.Fold"


@dataclass(frozen=True)
class FoldBacklog:
    sessions: int
    behind: int
    events_behind: int


class FoldRunner:
    def __init__(self, *, engine: AsyncEngine, history: Store, concurrency: int, page_size: int) -> None:
        """`engine` writes the fold tables; `history` reads the raw log, which this never writes."""
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._projector = HistoryProjector(engine, history, projection_epoch=FOLD_EPOCH, page_size=page_size)
        self._slots = asyncio.Semaphore(concurrency)
        self._leases: dict[UUID, ProjectionLease] = {}

    async def run(self, *, interval_s: float, changes: HistoryChanges, health: health.aio.HealthServicer) -> None:
        """Folds until cancelled, reporting `HEALTH_SERVICE` SERVING while it does. An error it does
        not retry stops the fold, not the process, so raw reads stay served: it is logged and
        `HEALTH_SERVICE` turns NOT_SERVING."""
        await health.set(HEALTH_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        try:
            await self._fold(interval_s=interval_s, changes=changes)
        except Exception:
            logger.exception("Thread fold stopped")
            await health.set(HEALTH_SERVICE, health_pb2.HealthCheckResponse.NOT_SERVING)

    async def _fold(self, *, interval_s: float, changes: HistoryChanges) -> None:
        """Catch up immediately while behind; wait for commits or `interval_s` to retry."""
        try:
            with changes.subscribe() as changed:
                while True:
                    changed.clear()
                    try:
                        backlog = await self.cycle()
                        logger.info(
                            "Thread fold: %d of %d Sessions behind, %d events behind",
                            backlog.behind,
                            backlog.sessions,
                            backlog.events_behind,
                        )
                    # asyncpg raises connect-time server errors (e.g. no free connection slots) unwrapped.
                    except SQLAlchemyError, PostgresError, OSError:
                        logger.warning("Thread fold cycle failed; will retry", exc_info=True)
                    else:
                        if backlog.behind:
                            continue
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(changed.wait(), timeout=interval_s)
        finally:
            await self.release()

    async def release(self) -> None:
        """Hand every held lease back, so another folder need not wait out `LEASE_DURATION`."""
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
        behind = [p for p in progress if p is not None and p.through_cursor < p.committed_cursor]
        return FoldBacklog(
            sessions=len(session_ids),
            behind=len(behind),
            events_behind=sum(p.committed_cursor - p.through_cursor for p in behind),
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
            # ValueError: history this fold cannot apply, such as an observation from a newer runner.
            except ValueError, ProjectionLeaseLostError, SQLAlchemyError, PostgresError, OSError:
                # One Session's unfoldable history or a lost lease or connection must not stop the
                # others; its checkpoint is retained and the next cycle retries it.
                logger.warning("Thread fold stalled for %s; will retry", session_id, exc_info=True)
                return None
            logger.debug(
                "Thread fold %s: through=%s committed=%s",
                session_id,
                progress.through_cursor,
                progress.committed_cursor,
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
