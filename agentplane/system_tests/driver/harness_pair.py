"""Run one case for each harness within one pytest fixture lifetime."""

from collections.abc import Awaitable, Callable

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.sandbox import Sandboxes, sandbox_scope
from agentplane.system_tests.driver.tracing import tracer

# gazelle:include_dep @pypi//protobuf


async def run_harness_pair(client: Client, run: Callable[[session_pb2.Harness, str], Awaitable[None]]) -> None:
    """Run Claude, then Codex; a failure stops the pair.

    The harnesses share the case's model server and stack. Running both at once would double the
    case's peak Sandbox count; a caller that creates Sandboxes uses `run_harness_pair_in_own_sandboxes`.
    """
    harnesses = [session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX]
    models = await models_offered_for(client, harnesses)
    for harness in harnesses:
        with tracer.start_as_current_span(
            "system_test.harness", attributes={"harness": session_pb2.Harness.Name(harness)}
        ):
            try:
                await run(harness, models[harness])
            except Exception as error:
                error.add_note(f"harness={session_pb2.Harness.Name(harness)}")
                raise


async def run_harness_pair_in_own_sandboxes(
    cluster: Cluster, client: Client, run: Callable[[session_pb2.Harness, str, Sandboxes], Awaitable[None]]
) -> None:
    """`run_harness_pair`, deleting each harness's Sandboxes before the next harness starts."""

    async def scoped(harness: session_pb2.Harness, model: str) -> None:
        async with sandbox_scope(cluster, client, tracer) as sandboxes:
            await run(harness, model, sandboxes)

    await run_harness_pair(client, scoped)
