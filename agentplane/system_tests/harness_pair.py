"""Run independent harness cases within one pytest fixture lifetime."""

import asyncio
from collections.abc import Awaitable, Callable

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.tracing import tracer

# gazelle:include_dep @pypi//protobuf


async def run_harness_pair(client: Client, run: Callable[[runner_pb2.Harness, str], Awaitable[None]]) -> None:
    harnesses = [runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX]
    models = await models_offered_for(client, harnesses)

    async def run_one(harness: runner_pb2.Harness) -> None:
        with tracer.start_as_current_span(
            "system_test.harness", attributes={"harness": runner_pb2.Harness.Name(harness)}
        ):
            try:
                await run(harness, models[harness])
            except Exception as error:
                error.add_note(f"harness={runner_pb2.Harness.Name(harness)}")
                raise

    # pytest-asyncio executes parametrized cases sequentially; xdist would duplicate
    # the session's k3s stack. Keep both harnesses on this case's live model server.
    async with asyncio.TaskGroup() as tasks:
        for harness in harnesses:
            tasks.create_task(run_one(harness), name=runner_pb2.Harness.Name(harness))
