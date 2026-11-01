"""Executable vm scenarios sharing one k3s stack."""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.k3s.vm_trace_contract import assert_vm_process_trace_contract
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.sandbox import VM_TEMPLATE
from agentplane.system_tests.scenario import ScenarioResources
from agentplane.system_tests.scenarios import vm_sandbox
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import traced_scenario
from agentplane.system_tests.vm_configuration import SUPERVISION_TEMPLATE
from util.testing.undeclared_outputs import undeclared_outputs_dir

# session_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


async def _wait_for_vm_process_trace_contract() -> None:
    output_dir = undeclared_outputs_dir()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    artifacts = [output_dir / "test-spans.jsonl", output_dir / "process-spans.jsonl"]
    last_sizes: tuple[int, ...] = ()
    error: AssertionError | None = None
    while True:
        current_sizes = tuple(path.stat().st_size if path.exists() else 0 for path in artifacts)
        if current_sizes != last_sizes:
            try:
                assert_vm_process_trace_contract(output_dir)
            except AssertionError as caught:
                error = caught
            else:
                return
            last_sizes = current_sizes
        if loop.time() >= deadline:
            raise AssertionError(f"VM process trace contract failed: {error}") from error
        await asyncio.sleep(0.5)


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_vm_scenarios(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    scenario_resources: ScenarioResources,
    scripted: ScriptedUpstream,
) -> None:
    harnesses = [session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX]
    models = await models_offered_for(client, harnesses)

    async def turn(harness: session_pb2.Harness, template: str) -> None:
        async with scenario_resources() as (owned_client, sandbox):
            await vm_sandbox.vm_sandbox_turn(
                cluster, owned_client, dispatcher, sandbox, harness, models[harness], scripted, template
            )

    async def oom() -> None:
        async with scenario_resources() as (owned_client, sandbox):
            await vm_sandbox.codex_tool_oom(
                cluster,
                owned_client,
                dispatcher,
                sandbox,
                models[session_pb2.HARNESS_CODEX],
                scripted,
                native_profile=os.environ.get("AGENTPLANE_VM_NATIVE_PROFILE") == "1",
            )

    # pytest-asyncio serializes separate tests. Launch every independent boot here so a
    # finished scenario never leaves its worker capacity idle behind another test's teardown.
    # Each task owns its guests and cleanup.
    async with asyncio.TaskGroup() as scenarios:
        # Only Claude selects the private supervisor checks; both use the ordinary guest image.
        scenarios.create_task(
            traced_scenario("vm-turn-harness_claude", turn(session_pb2.HARNESS_CLAUDE, SUPERVISION_TEMPLATE))
        )
        scenarios.create_task(traced_scenario("vm-turn-harness_codex", turn(session_pb2.HARNESS_CODEX, VM_TEMPLATE)))
        scenarios.create_task(traced_scenario("codex-tool-oom", oom()))

    await _wait_for_vm_process_trace_contract()


if __name__ == "__main__":
    pytest_bazel.main()
