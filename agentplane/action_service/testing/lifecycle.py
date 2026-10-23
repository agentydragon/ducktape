"""Condition-based waiting for replica-local MCP discovery and Action request state in integration tests."""

from uuid import UUID

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.action_service.models import ActionRequestView, ActionState, CallerPrincipal
from agentplane.action_service.service import ActionService
from agentplane.action_service.store import ActionStore


async def wait_for_state(
    source: ActionStore | ActionService, request_id: UUID, caller: CallerPrincipal, *, want: ActionState
) -> ActionRequestView:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            view = await source.get(request_id, caller)
            assert view.state is want
    return view
