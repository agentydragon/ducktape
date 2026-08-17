"""What buys a session its sandbox: a prompt nobody has claimed.

**One rule, one implementation, every surface.** A session is created idle — a row, no
credential, no `SandboxClaim` — whether the browser posted for a conversation or a room was
bound to one, and this sweep is the only thing that turns demand into a claim
(<README.md> § An idle session). A channel's supervisor creates the row and says what it sees;
it does not decide this.

**Never on the request path.** `allocate` talks to Kubernetes, so deciding inside
`POST /api/sessions/{session_id}/messages` would make the operator's first message wait out a
cold start, and would leave a failure with nowhere to go. Here it has one: `SessionService.allocate`
records the failure on the row and removes whatever claim it managed to make, and the next pass
reads whatever is still waiting.

**One replica sweeps**, under an advisory lock of its own rather than a channel's — the two need
single execution but not co-location, and a pass that is slow talking to Kubernetes must not be
what costs a room its supervisor.

Waking on `SessionEventKind.PROMPT` is what keeps the first message after quiet from paying an
interval on top of the cold start it already pays. The interval is the backstop for what no
notification carries: a listener reconnect drops the ids notified while it was down
(`SessionNotifications.watch`), which for this consumer must only ever be a delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import REPLICA, SessionStore

logger = logging.getLogger(__name__)

# Distinct from the Matrix supervisor's lock and the index sync's two.
ALLOCATOR_ADVISORY_LOCK = 0x5342_4F58  # "SBOX"

# The backstop for a `PROMPT` notification this replica never received.
SWEEP_INTERVAL = timedelta(seconds=10)
# How long a replica that lost the election waits before contending again.
LEADER_RETRY = timedelta(seconds=30)
# A failed pass must not become a hot loop against Kubernetes.
FAILURE_BACKOFF = timedelta(seconds=60)


class SandboxAllocator:
    """Gives a sandbox to every session that has demand and none, whatever surface created it."""

    def __init__(
        self, service: SessionService, store: SessionStore, notifications: SessionNotifications, engine: AsyncEngine
    ):
        self._service = service
        self._store = store
        self._notifications = notifications
        self._engine = engine
        self._prompted = asyncio.Event()

    async def allocate_once(self) -> None:
        """Allocate for everything waiting, one session at a time.

        No guard against a session that has already been allocated: `SessionStore.allocate` is the
        transition that decides, so a second caller — another replica mid-handover, this pass
        racing a retry — gets `None` back and makes no claim of its own.
        """
        for session_id in await self._store.sessions_awaiting_sandbox():
            try:
                await self._service.allocate(session_id)
            except Exception:
                # The row already says why (`SessionService.allocate` fails the session and
                # removes the partial claim); what is left is not to lose the rest of the pass.
                logger.exception("allocating a sandbox for session %s failed", session_id)
                continue
            logger.info("a queued prompt bought session %s a sandbox", session_id)

    def _wake(self, session_id: UUID) -> None:
        """Runs on the listener's reader task: note it and let the sweep do the work."""
        del session_id  # Every prompt is read out of the database by the pass this wakes.
        self._prompted.set()

    async def _sweep(self) -> None:
        """Sweep until cancelled. Only ever entered holding the advisory lock."""
        while True:
            # Cleared before the pass rather than after, so a prompt queued while it runs wakes
            # the next one instead of being folded into the pass that had already read the row.
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
                logger.info("this replica (%s) allocates sandboxes", REPLICA)
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
        with self._notifications.watch(SessionEventKind.PROMPT, self._wake):
            task = asyncio.create_task(self._run(), name="sandbox-allocator")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
