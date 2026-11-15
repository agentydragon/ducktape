"""Run one case for each harness within one pytest fixture lifetime."""

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import native_environment_client
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

    # TODO: Both harnesses could reuse one Sandbox to save a start per case, as the `shared()` pool does;
    #   prefer moving `open()` cases that need no bespoke Sandbox onto `shared()`. Blockers: inbox/ack
    #   cursors carry over, mutating cases (a policy granted while running) don't fit, and the second
    #   harness could pass on state the first left behind.
    async def scoped(harness: session_pb2.Harness, model: str) -> None:
        test_tmpdir = Path(os.environ["TEST_TMPDIR"])
        if not test_tmpdir.is_absolute():
            raise RuntimeError("TEST_TMPDIR must be absolute for the native Gateway token")
        with TemporaryDirectory(prefix="agentplane-gateway-", dir=test_tmpdir) as directory:
            async with (
                native_environment_client(cluster, Path(directory) / "gateway-token") as environments,
                sandbox_scope(cluster, environments, tracer) as sandboxes,
            ):
                await run(harness, model, sandboxes)

    await run_harness_pair(client, scoped)
