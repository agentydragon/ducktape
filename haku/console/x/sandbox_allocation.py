"""Reconcile durable prompt demand into sandbox allocation, independent of its channel.

A session begins idle: it has a row, but no runner credential and no ``SandboxClaim``. The first
accepted prompt is the durable fact that buys one. This module is the only background path that
turns that fact into provisioning, whether the prompt arrived through the SPA, Matrix, or a future
surface.

The allocator is deliberately outside request and channel-supervisor paths. A prompt commit must
survive the replica that accepted it, and a slow or failed Kubernetes write must not hold open an
HTTP response or stop Matrix ingress. One replica sweeps under the ``SBOX`` advisory lock;
``PROMPT`` notifications provide low-latency wakes and a periodic pass recovers notifications lost
during listener reconnects or process restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import REPLICA, SessionStore
from haku.console.x.session_wakes import SessionEvent, SessionEventKind, SessionWakes

logger = logging.getLogger(__name__)

ALLOCATOR_ADVISORY_LOCK = 0x5342_4F58  # "SBOX"
SWEEP_INTERVAL = timedelta(seconds=10)
LEADER_RETRY = timedelta(seconds=30)
FAILURE_BACKOFF = timedelta(seconds=60)


class SandboxAllocator:
    """Give every idle session with queued work its sandbox, whichever surface created it."""

    def __init__(self, service: SessionService, store: SessionStore, notifications: SessionWakes, engine: AsyncEngine):
        self._service = service
        self._store = store
        self._notifications = notifications
        self._engine = engine
        self._prompted = asyncio.Event()

    async def allocate_once(self) -> None:
        """Serve one oldest-first snapshot of durable demand without letting one failure stop it.

        The snapshot is advisory. ``SessionStore.allocate`` takes the session row lock and repeats
        the state-and-demand check, so competing replicas or a prompt notification racing the
        periodic pass still create exactly one claim.
        """
        for demand in await self._store.sessions_awaiting_sandbox():
            try:
                allocated = await self._service.allocate(demand.operator_id, demand.session_id)
            except Exception:
                # SessionService records the failure and reconciles an ambiguously created claim.
                # Continue so one broken Kubernetes object cannot starve unrelated conversations.
                logger.exception("allocating a sandbox for session %s failed", demand.session_id)
                continue
            if allocated:
                logger.info("a queued prompt bought session %s a sandbox", demand.session_id)

    def _wake(self, event: SessionEvent) -> None:
        """Wake the database sweep on prompt demand; the listener callback must not block or await.

        Only `prompt` names new demand — `update` fires per streaming delta, and sweeping on those
        would run continuously. The sweep reads every durable demand row, so which session the
        wake named does not matter.
        """
        if event.kind is SessionEventKind.PROMPT:
            self._prompted.set()

    async def _sweep(self) -> None:
        """Sweep until cancelled. Only entered while this replica holds the allocator lock."""
        while True:
            # Clear before reading: a prompt committed during the pass must wake the next pass,
            # rather than being mistaken for one already covered by this snapshot.
            self._prompted.clear()
            try:
                await self.allocate_once()
            except Exception:
                logger.exception("the sandbox allocation sweep failed")
                await asyncio.sleep(FAILURE_BACKOFF.total_seconds())
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(SWEEP_INTERVAL.total_seconds()):
                    await self._prompted.wait()

    async def _run(self) -> None:
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(
                    text("SELECT pg_try_advisory_lock(:lock)"), {"lock": ALLOCATOR_ADVISORY_LOCK}
                ):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("this replica (%s) is the sandbox allocator", REPLICA)
                try:
                    await self._sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("the sandbox allocator exited, retrying")
                    await asyncio.sleep(FAILURE_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": ALLOCATOR_ADVISORY_LOCK})

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the elected allocator and register its notification wake for this replica."""
        with self._notifications.watch(self._wake):
            task = asyncio.create_task(self._run(), name="sandbox-allocator")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
