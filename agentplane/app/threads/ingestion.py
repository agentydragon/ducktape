"""Lease-coordinated UI projection from Sandbox Service history."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from uuid import UUID

import grpc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.threads.events import projection_lease
from agentplane.app.threads.events.event_log import EventLogStore, EventReplicationError, RunnerSession
from agentplane.app.threads.events.projection_lease import ProjectionLease, ProjectionLeaseLostError
from agentplane.app.threads.history_projector import HistoryProjector
from agentplane.app.threads.sessions import SandboxNotReachableError, SandboxSessions
from agentplane.sandbox_service.models import SandboxNotFoundError

# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
RECONCILE_S: float = 2
LEASE_DURATION = timedelta(seconds=30)


class Ingestion:
    """Manage sandbox leases shared by app projection replicas."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def acquire(self, sandbox: str, duration: timedelta) -> ProjectionLease | None:
        async with self._sessions.begin() as session:
            return await projection_lease.acquire(session, sandbox, duration)

    async def renew(self, lease: ProjectionLease, duration: timedelta) -> bool:
        async with self._sessions.begin() as session:
            return await projection_lease.renew(session, lease, duration)

    async def release(self, lease: ProjectionLease) -> None:
        async with self._sessions.begin() as session:
            await projection_lease.release(session, lease)


class Ingester:
    """Discover Sessions and project their service-owned history under sandbox leases."""

    def __init__(
        self,
        *,
        runners: SandboxSessions,
        event_logs: EventLogStore,
        ingestion: Ingestion,
        history_projector: HistoryProjector,
    ) -> None:
        self._runners = runners
        self._history_projector = history_projector
        self._event_logs = event_logs
        self._ingestion = ingestion
        self._leases: dict[str, ProjectionLease] = {}
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._coordinator: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the ingestion coordinator, or wake it to reconcile now."""
        if self._coordinator is None:
            self._coordinator = asyncio.create_task(self._coordinate(), name="sandbox-ingestion")
        self._changed.set()

    async def _coordinate(self) -> None:
        with self._runners.changes.subscribe(self._changed):
            await self._coordinate_subscribed()

    async def _coordinate_subscribed(self) -> None:
        while True:
            self._changed.clear()
            try:
                await self.reconcile()
            except SQLAlchemyError, grpc.aio.AioRpcError, OSError:
                logger.warning("sandbox ingestion reconciliation failed; will retry", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=RECONCILE_S)

    async def reconcile(self) -> None:
        """Renew ownership of the running sandboxes and discover sessions opened through any replica."""
        async with self._reconcile_lock:
            running = set(self._runners.running())
            projectable = await self._event_logs.projection_sessions()
            running.update(locator.sandbox for locator in projectable.values())
            for sandbox in set(self._leases) - running:
                await self._release(sandbox)
            async with asyncio.TaskGroup() as tasks:
                for sandbox in sorted(running):
                    tasks.create_task(self._reconcile_sandbox(sandbox, projectable))

    async def _reconcile_sandbox(self, sandbox: str, projectable: dict[UUID, RunnerSession]) -> None:
        try:
            async with asyncio.timeout(10):
                lease = self._leases.get(sandbox)
                if lease is not None and not await self._ingestion.renew(lease, LEASE_DURATION):
                    await self._release(sandbox)
                    lease = None
                if lease is None:
                    lease = await self._ingestion.acquire(sandbox, LEASE_DURATION)
                    if lease is None:
                        return
                    self._leases[sandbox] = lease
                selected = {thread: locator for thread, locator in projectable.items() if locator.sandbox == sandbox}
                async with asyncio.TaskGroup() as tasks:
                    for thread_id in selected:
                        tasks.create_task(self._project_history(thread_id, lease))
                if sandbox not in self._runners.running():
                    return
                try:
                    async with asyncio.timeout(5):
                        client = self._runners.client(sandbox)
                        summaries = await client.list_sessions()
                    for summary in summaries:
                        try:
                            await self._event_logs.open(sandbox, summary.session_id, summary.spec)
                        except EventReplicationError:
                            logger.warning(
                                "session %s/%s requires explicit history reconciliation", sandbox, summary.session_id
                            )
                            continue
                except (
                    grpc.aio.AioRpcError,
                    ConnectionError,
                    SandboxNotReachableError,
                    SandboxNotFoundError,
                    TimeoutError,
                ):
                    logger.warning("sandbox %s ingestion discovery unavailable", sandbox, exc_info=True)
        except SQLAlchemyError, OSError, TimeoutError:
            logger.warning("sandbox %s ingestion reconciliation failed; will retry", sandbox, exc_info=True)

    async def _project_history(self, thread_id: UUID, lease: ProjectionLease) -> None:
        try:
            progress = await self._history_projector.project_batch(thread_id, lease=lease)
            logger.debug(
                "service projection %s: through=%s service=%s",
                thread_id,
                progress.through_cursor,
                progress.service_cursor,
            )
        except (
            EventReplicationError,
            ProjectionLeaseLostError,
            SQLAlchemyError,
            grpc.aio.AioRpcError,
            ConnectionError,
            ValueError,
        ):
            # One failed UI fold must not stop other Threads or the service archive.
            logger.warning("service projection stalled for %s; checkpoint retained", thread_id, exc_info=True)

    async def _release(self, sandbox: str) -> None:
        await self._ingestion.release(self._leases.pop(sandbox))

    async def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._coordinator
            self._coordinator = None
        for sandbox in list(self._leases):
            await self._release(sandbox)
