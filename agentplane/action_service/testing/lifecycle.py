"""Condition-based waiting for replica-local MCP discovery and Action request state in integration tests."""

from uuid import UUID

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.models import ActionRequestView, ActionState, CallerPrincipal
from agentplane.action_service.service import ActionService
from agentplane.action_service.store import ActionStore


async def wait_available(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.available


async def wait_retry(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.health is not None
            assert group.health.failures > 0


async def wait_for_state(
    source: ActionStore | ActionService, request_id: UUID, caller: CallerPrincipal, *, want: ActionState
) -> ActionRequestView:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            view = await source.get(request_id, caller)
            assert view.state is want
    return view
