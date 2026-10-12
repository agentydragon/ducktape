"""Discover the Sessions running Sandboxes hold and link each to an app Thread.

A Session's history and its fold belong to the History Service; the app only opens the Thread.
"""

import asyncio
import contextlib
import logging
from types import TracebackType
from typing import Self

import grpc
from sqlalchemy.exc import SQLAlchemyError

from agentplane.app.threads.events.event_log import EventLogStore, SessionAdmissionError
from agentplane.app.threads.sessions import SandboxNotReachableError, SandboxSessions
from agentplane.sandbox_service.errors import SandboxNotFoundError

# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
RECONCILE_S: float = 2


class SessionDiscovery:
    def __init__(self, *, runners: SandboxSessions, event_logs: EventLogStore) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._coordinator: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._coordinator is not None:
            raise RuntimeError("session discovery already running")
        self._coordinator = asyncio.create_task(self._coordinate(), name="session-discovery")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    def wake(self) -> None:
        """Reconcile now rather than at the next interval."""
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
                logger.warning("session discovery failed; will retry", exc_info=True)
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
                        await self._event_logs.open(summary.session_id)
                    except SessionAdmissionError:
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
        """Stop discovery; idempotent, so a replica can stop before its context exits."""
        coordinator, self._coordinator = self._coordinator, None
        if coordinator is not None:
            coordinator.cancel()
            await asyncio.wait({coordinator})
            if not coordinator.cancelled() and (error := coordinator.exception()) is not None:
                raise error
