"""Client and Sandbox ownership for one concurrent scenario on a shared deployment."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.system_tests.driver import native_artifacts
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes, sandbox_scope

type ScenarioResources = Callable[[], AbstractAsyncContextManager[tuple[Client, Sandboxes]]]


@asynccontextmanager
async def open_resources(
    cluster: Cluster, base_url: str, token: str, phase_tracer: trace.Tracer, *, native_diagnostics: bool = True
) -> AsyncIterator[tuple[Client, Sandboxes]]:
    async with AsyncExitStack() as resources:
        client = await resources.enter_async_context(Client.connect(base_url=base_url, token=token))
        sandbox = await resources.enter_async_context(sandbox_scope(cluster, client, phase_tracer))

        if native_diagnostics:

            async def diagnostics() -> None:
                for uid in client.opened_sandbox_uids:
                    with phase_tracer.start_as_current_span(
                        "system_test.diagnostics", attributes={"sandbox.uid": str(uid)}
                    ):
                        await native_artifacts.capture(client, sandbox_uid=uid)

            # ExitStack runs diagnostics before deleting guests, and closes their client last.
            resources.push_async_callback(diagnostics)
        error: BaseException | None = None
        try:
            yield client, sandbox
        except BaseException as caught:
            error = caught
            raise
        finally:
            # A peer can fail while this scenario is already cleaning up. Finish teardown
            # before propagating TaskGroup cancellation; the stack must outlive every guest.
            closing = asyncio.create_task(
                resources.pop_all().__aexit__(
                    type(error) if error is not None else None,
                    error,
                    error.__traceback__ if error is not None else None,
                )
            )
            cancellation: asyncio.CancelledError | None = None
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as caught:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        cancellation = cancellation or caught
                except BaseException:
                    break
            try:
                closing.result()
            except BaseException as cleanup_error:
                if error is not None:
                    error.add_note(f"Scenario cleanup also failed ({type(cleanup_error).__name__}).")
                elif cancellation is not None:
                    raise cancellation from cleanup_error
                else:
                    raise
            if error is None and cancellation is not None:
                raise cancellation
