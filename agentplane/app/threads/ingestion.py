"""Discover the Sessions running Sandboxes hold and link each to an app Thread.

A Session's history and its fold belong to the History Service; the app only opens the Thread.
"""

import asyncio
import contextlib
import logging

import grpc
from sqlalchemy.exc import SQLAlchemyError

from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.sessions import SandboxNotReachableError, SandboxSessions
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.thread_fold.recording import EventReplicationError

# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
RECONCILE_S: float = 2


class Ingester:
    def __init__(self, *, runners: SandboxSessions, event_logs: EventLogStore) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._coordinator: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start discovery, or wake it to reconcile now."""
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
        """Open a Thread for every Session of a running Sandbox that has none yet."""
        async with self._reconcile_lock, asyncio.TaskGroup() as tasks:
            for sandbox in sorted(self._runners.running()):
                tasks.create_task(self._discover_sessions(sandbox))

    async def _discover_sessions(self, sandbox: str) -> None:
        try:
            async with asyncio.timeout(5):
                summaries = await self._runners.client(sandbox).list_sessions()
                for summary in summaries:
                    try:
                        await self._event_logs.open(sandbox, summary.session_id)
                    except EventReplicationError:
                        logger.warning(
                            "session %s/%s requires explicit history reconciliation", sandbox, summary.session_id
                        )
        except (
            grpc.aio.AioRpcError,
            ConnectionError,
            SandboxNotReachableError,
            SandboxNotFoundError,
            TimeoutError,
            SQLAlchemyError,
        ):
            logger.warning("sandbox %s Session discovery unavailable", sandbox, exc_info=True)

    async def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._coordinator
            self._coordinator = None
