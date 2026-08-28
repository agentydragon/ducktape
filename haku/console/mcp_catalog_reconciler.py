"""Continuously reconcile per-Operator MCP tool catalogs ahead of discovery requests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

from haku.console.mcp_approval import (
    DegradedReflection,
    McpServerDispatcher,
    ReflectionFailureStage,
    ServerReflection,
    metadata_for_operator,
)
from haku.console.mcp_config import McpServerEntry
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.mcp_reflection_cache import ReflectedCatalog
from haku.console.notifications.console_events import (
    ConsoleEvent,
    McpOperatorAuthChangedEvent,
    OperatorConnectionChangedEvent,
)
from haku.console.tool_call_service import ProviderConnectionTokenStore

logger = logging.getLogger(__name__)

OperatorIds = Callable[[], Awaitable[list[UUID]]]


def _detached(reflection: ServerReflection) -> ServerReflection:
    if isinstance(reflection, DegradedReflection):
        return reflection
    return ReflectedCatalog(
        tools=[tool.model_copy(deep=True) for tool in reflection.tools], instructions=reflection.instructions
    )


class OperatorCatalogReconciler:
    """Refresh catalogs in the background and serve request-time snapshots without upstream I/O.

    One atomic snapshot is retained per Operator and configured server. A full reconciliation runs
    before the MCP endpoint becomes ready, then repeats for the process lifetime. Newly admitted
    Operators are scheduled when first observed; their first listing is empty rather than becoming
    an accidental synchronous catalog load.
    """

    def __init__(
        self,
        *,
        servers: list[McpServerEntry],
        dispatcher: McpServerDispatcher,
        oauth_store: PostgresMcpOperatorOAuthStore,
        provider_store: ProviderConnectionTokenStore,
        operator_ids: OperatorIds,
        refresh_interval_seconds: float,
    ) -> None:
        self._servers = servers
        self._dispatcher = dispatcher
        self._oauth_store = oauth_store
        self._provider_store = provider_store
        self._operator_ids = operator_ids
        self._refresh_interval_seconds = refresh_interval_seconds
        self._snapshots: dict[tuple[UUID, str], ServerReflection] = {}
        self._generations: dict[UUID, int] = {}
        self._scheduled: dict[UUID, asyncio.Task[None]] = {}

    def metadata(self, *, operator_id: UUID, server: McpServerEntry) -> ServerReflection:
        reflection = self._snapshots.get((operator_id, server.id))
        if reflection is None:
            self.schedule(operator_id)
            return DegradedReflection(
                failure_stage=ReflectionFailureStage.TOOL_DISCOVERY,
                degraded_reason="the background catalog reconciler has not published this catalog yet",
            )
        return _detached(reflection)

    def schedule(self, operator_id: UUID) -> None:
        """Queue an unseen Operator without making its discovery request wait for reflection."""
        if any(key_operator_id == operator_id for key_operator_id, _ in self._snapshots):
            return
        existing = self._scheduled.get(operator_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self.refresh_operator(operator_id))
        self._scheduled[operator_id] = task

        def done_callback(done: asyncio.Task[None]) -> None:
            self._scheduled_done(operator_id, done)

        task.add_done_callback(done_callback)

    def connection_changed(self, operator_id: UUID, event: ConsoleEvent) -> None:
        """Invalidate authority-sensitive discovery immediately, then rebuild it off-path."""
        if not isinstance(event, McpOperatorAuthChangedEvent | OperatorConnectionChangedEvent):
            return
        self._generations[operator_id] = self._generations.get(operator_id, 0) + 1
        self._snapshots = {key: reflection for key, reflection in self._snapshots.items() if key[0] != operator_id}
        if (scheduled := self._scheduled.pop(operator_id, None)) is not None:
            scheduled.cancel()
        self.schedule(operator_id)

    def _scheduled_done(self, operator_id: UUID, task: asyncio.Task[None]) -> None:
        if self._scheduled.get(operator_id) is task:
            self._scheduled.pop(operator_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "MCP catalog reconciliation failed for Operator %s",
                operator_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def reconcile(self) -> None:
        operator_ids = set(await self._operator_ids())
        await asyncio.gather(*(self.refresh_operator(operator_id) for operator_id in operator_ids))
        self._snapshots = {key: reflection for key, reflection in self._snapshots.items() if key[0] in operator_ids}
        self._generations = {
            operator_id: generation
            for operator_id, generation in self._generations.items()
            if operator_id in operator_ids
        }

    async def refresh_operator(self, operator_id: UUID) -> None:
        """Reflect and atomically publish one Operator, for periodic and connection-change callers."""
        generation = self._generations.get(operator_id, 0)
        reflections = await asyncio.gather(
            *(self._reflect(operator_id=operator_id, server=server) for server in self._servers)
        )
        if generation != self._generations.get(operator_id, 0):
            return
        # Publish the Operator's whole catalog at once: a listing sees either the previous complete
        # generation or the new one, never a mix assembled while upstreams finish out of order.
        replacement = {
            (operator_id, server.id): _detached(reflection)
            for server, reflection in zip(self._servers, reflections, strict=True)
        }
        self._snapshots = {
            key: reflection for key, reflection in self._snapshots.items() if key[0] != operator_id
        } | replacement

    async def _reflect(self, *, operator_id: UUID, server: McpServerEntry) -> ServerReflection:
        try:
            return await metadata_for_operator(
                operator_id=operator_id,
                server=server,
                dispatcher=self._dispatcher,
                oauth_store=self._oauth_store,
                provider_store=self._provider_store,
            )
        except Exception as error:
            logger.exception("MCP catalog reconciliation failed for server %s", server.id)
            return DegradedReflection(failure_stage=ReflectionFailureStage.TOOL_DISCOVERY, degraded_reason=str(error))

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_seconds)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("MCP catalog reconciliation pass failed")

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        await self.reconcile()
        refresh_task = asyncio.create_task(self._refresh_loop())
        try:
            yield
        finally:
            refresh_task.cancel()
            for task in self._scheduled.values():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
            await asyncio.gather(*self._scheduled.values(), return_exceptions=True)
            self._scheduled.clear()
