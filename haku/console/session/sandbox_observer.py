"""Leader-elected Kubernetes invalidations for the active sandbox inventory.

Kubernetes is the source of truth for a claim's provisioning graph, while the Console event socket
is deliberately only a lossy wake. One replica watches the claim, Sandbox, Pod, and runner graph;
every replica's connected browsers receive an operator-scoped invalidation and read the graph again
through the stateless MCP endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.console.notifications.console_events import ConsoleEventHub, SandboxSessionsChangedEvent
from haku.console.session.runtime import SessionService
from haku.console.session.sandbox_claims import SandboxClaims
from haku.console.session.store import REPLICA

logger = logging.getLogger(__name__)

OBSERVER_ADVISORY_LOCK = 0x5342_4F42  # "SBOB"
LEADER_RETRY_SECONDS = 30
WATCH_FAILURE_RETRY_SECONDS = 5


class SandboxSessionObserver:
    """Watch one elected replica's claim clients and fan out inventory invalidations."""

    def __init__(
        self,
        service: SessionService,
        claims: Iterable[SandboxClaims],
        engine: AsyncEngine,
        event_hub: ConsoleEventHub,
        operator_ids: Callable[[], Awaitable[list]],
    ) -> None:
        self._service = service
        self._claims = tuple(dict.fromkeys(claims))
        self._engine = engine
        self._event_hub = event_hub
        self._operator_ids = operator_ids

    async def _publish(self) -> None:
        self._service.invalidate_sandbox_observations()
        try:
            operator_ids = await self._operator_ids()
        except Exception:
            logger.exception("could not enumerate Operators for sandbox inventory invalidation")
            return
        await asyncio.gather(
            *(self._event_hub.broadcast(operator_id, [SandboxSessionsChangedEvent()]) for operator_id in operator_ids)
        )

    async def _watch(self, claim: SandboxClaims, queue: asyncio.Queue[None], stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async for _change in claim.watch_changes(stop):
                    if stop.is_set():
                        return
                    queue.put_nowait(None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sandbox resource watch failed; retrying")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=WATCH_FAILURE_RETRY_SECONDS)

    async def _observe(self, stop: asyncio.Event) -> None:
        queue: asyncio.Queue[None] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._watch(claim, queue, stop), name="sandbox-session-watch") for claim in self._claims
        ]
        try:
            while not stop.is_set():
                get_change = asyncio.create_task(queue.get())
                stop_wait = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait((get_change, stop_wait), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_wait in done:
                    return
                if get_change in done:
                    # A claim update is commonly followed immediately by Sandbox and Pod updates.
                    # Coalesce that burst into one invalidation while retaining the lossy-channel
                    # contract: the next read observes the whole graph.
                    await asyncio.sleep(0)
                    while not queue.empty():
                        queue.get_nowait()
                    await self._publish()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self) -> None:
        stop = asyncio.Event()
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(
                    text("SELECT pg_try_advisory_lock(:lock)"), {"lock": OBSERVER_ADVISORY_LOCK}
                ):
                    await asyncio.sleep(LEADER_RETRY_SECONDS)
                    continue
                logger.info("this replica (%s) is the sandbox session observer", REPLICA)
                try:
                    await self._observe(stop)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("sandbox session observer exited; retrying")
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": OBSERVER_ADVISORY_LOCK})

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the observer until the application lifespan exits."""
        task = asyncio.create_task(self._run(), name="sandbox-session-observer")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
