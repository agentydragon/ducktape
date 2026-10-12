"""Condition-based waiting for replica-local MCP discovery and Action request state in integration tests."""

from uuid import UUID

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.db import ActionStore
from agentplane.action_service.models import ActionState, CallerPrincipal


async def wait_available(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.available


async def wait_retry(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.health is not None
            assert group.health.failures > 0


async def wait_for_state(store: ActionStore, request_id: UUID, caller: CallerPrincipal, *, want: ActionState) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert (await store.get(request_id, caller)).state is want
