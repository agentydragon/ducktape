"""Client and Sandbox ownership for one concurrent scenario on a shared deployment."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver import native_artifacts
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import native_environment_client
from agentplane.system_tests.driver.sandbox import Sandboxes, sandbox_scope

type ScenarioResources = Callable[[], AbstractAsyncContextManager[tuple[Client, Sandboxes]]]


@asynccontextmanager
async def open_resources(
    cluster: Cluster,
    base_url: str,
    token: str,
    phase_tracer: trace.Tracer,
    *,
    token_file: Path,
    native_diagnostics: bool = True,
) -> AsyncIterator[tuple[Client, Sandboxes]]:
    async with AsyncExitStack() as resources:
        client = await resources.enter_async_context(Client.connect(base_url=base_url, token=token))
        token_directory = Path(
            resources.enter_context(TemporaryDirectory(prefix="agentplane-gateway-", dir=token_file.parent))
        )
        environments = await resources.enter_async_context(
            native_environment_client(cluster, token_directory / "gateway-token")
        )
        sandbox = await resources.enter_async_context(sandbox_scope(cluster, environments, phase_tracer))

        if native_diagnostics:

            async def diagnostics() -> None:
                for uid in sandbox.opened_environment_uids:
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
