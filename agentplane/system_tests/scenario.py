"""Client and Sandbox ownership for one concurrent scenario on a shared deployment."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.system_tests import native_artifacts
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import Sandboxes, sandbox_scope

type ScenarioResources = Callable[[], AbstractAsyncContextManager[tuple[Client, Sandboxes]]]


@asynccontextmanager
async def open_resources(
    cluster: Cluster, base_url: str, token: str, phase_tracer: trace.Tracer
) -> AsyncIterator[tuple[Client, Sandboxes]]:
    async with AsyncExitStack() as resources:
        client = await resources.enter_async_context(Client(base_url=base_url, token=token))
        sandbox = await resources.enter_async_context(sandbox_scope(cluster, client, phase_tracer))

        async def diagnostics() -> None:
            for uid in client.opened_sandbox_uids:
                with phase_tracer.start_as_current_span(
                    "system_test.diagnostics", attributes={"sandbox.uid": str(uid)}
                ):
                    await native_artifacts.capture(client, sandbox_uid=uid)

        # ExitStack runs diagnostics before deleting guests, and closes their client last.
        resources.push_async_callback(diagnostics)
        try:
            yield client, sandbox
        finally:
            # A peer can fail while this scenario is already cleaning up. Finish teardown
            # before propagating TaskGroup cancellation; the stack must outlive every guest.
            closing = asyncio.create_task(resources.pop_all().aclose())
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await closing
                raise
