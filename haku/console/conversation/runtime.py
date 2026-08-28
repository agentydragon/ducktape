"""Reconcile conversation-owned prompt demand into sessions, independent of every channel.

A conversation may exist without a session. Its first accepted prompt is the durable fact that asks
for runtime capacity: this elected reconciler creates the one idle session that can serve it, then
the separate ``SandboxAllocator`` turns that session's same prompt demand into a container.

Session maintenance belongs here for the same reason. Lease expiry, terminal-claim cleanup and
replacement must keep working for browser-only conversations and for conversations with no channel
attachment at all; Matrix is only one renderer of the lifecycle facts they record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.console.notifications.conversation_wakes import (
    ConversationWakeEvent,
    ConversationWakeKind,
    ConversationWakes,
    RecheckHeld,
)
from haku.console.session.runtime import SessionService
from haku.console.session.store import REPLICA, Store

logger = logging.getLogger(__name__)

RUNTIME_ADVISORY_LOCK = 0x4352_554E  # "CRUN"
SWEEP_INTERVAL = timedelta(seconds=10)
LEADER_RETRY = timedelta(seconds=30)
FAILURE_BACKOFF = timedelta(seconds=60)


class Runtime:
    """Create or replace sessions for durable conversation demand and maintain them globally."""

    def __init__(
        self, service: SessionService, store: Store, notifications: ConversationWakes, engine: AsyncEngine
    ) -> None:
        self._service = service
        self._store = store
        self._notifications = notifications
        self._engine = engine
        self._demanded = asyncio.Event()

    async def reconcile_once(self) -> None:
        """Run one maintenance pass, isolating external cleanup from session creation."""
        try:
            await self._store.expire_stale_leases()
        except Exception:
            logger.exception("expiring stale runtime leases failed")
        try:
            await self._service.reconcile_terminal_claims()
        except Exception:
            logger.exception("reconciling terminal sandbox claims failed")

        for demand in await self._store.conversations_awaiting_session():
            try:
                created = await self._service.ensure_session_for_demand(demand.operator_id, demand.conversation_id)
            except Exception:
                logger.exception("creating a session for conversation %s failed", demand.conversation_id)
                continue
            if created is not None:
                logger.info("queued conversation %s opened idle session %s", demand.conversation_id, created.session_id)

    def _wake(self, wake: ConversationWakeEvent | RecheckHeld) -> None:
        """Wake the durable sweep on demand; the listener callback itself cannot await.

        Only `runtime_demand` (and a gap's `RecheckHeld`) wakes it: `update` fires per streaming
        delta, and sweeping on those would run the reconciler continuously. The sweep reads every
        durable demand row, so which conversation the wake named does not matter.
        """
        match wake:
            case ConversationWakeEvent(kind=ConversationWakeKind.RUNTIME_DEMAND) | RecheckHeld():
                self._demanded.set()
            case ConversationWakeEvent():
                pass

    async def _sweep(self) -> None:
        while True:
            self._demanded.clear()
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("the conversation runtime sweep failed")
                await asyncio.sleep(FAILURE_BACKOFF.total_seconds())
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(SWEEP_INTERVAL.total_seconds()):
                    await self._demanded.wait()

    async def _run(self) -> None:
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": RUNTIME_ADVISORY_LOCK}):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("this replica (%s) supervises conversation runtimes", REPLICA)
                try:
                    await self._sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("the conversation runtime exited, retrying")
                    await asyncio.sleep(FAILURE_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": RUNTIME_ADVISORY_LOCK})

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the elected reconciler and register this replica's low-latency demand wake."""
        with self._notifications.watch(self._wake):
            task = asyncio.create_task(self._run(), name="conversation-runtime")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
