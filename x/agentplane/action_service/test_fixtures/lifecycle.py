"""Condition-based waiting for replica-local MCP discovery in integration tests."""

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from x.agentplane.action_service.catalog import ActionGroup


async def wait_available(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.available


async def wait_retry(group: ActionGroup) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
        with attempt:
            assert group.health is not None
            assert group.health.failures > 0
