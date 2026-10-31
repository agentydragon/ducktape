"""Executable vm scenarios sharing one k3s stack."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.executor import Executor
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.sandbox import VM_TEMPLATE
from agentplane.system_tests.scenario import ScenarioResources
from agentplane.system_tests.scenarios import central_sampler, runner_credentials, vm_sandbox
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import traced_scenario
from agentplane.system_tests.vm_configuration import SUPERVISION_TEMPLATE

# runner_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_vm_scenarios(
    cluster: Cluster, tmp_path: Path, client: Client, scenario_resources: ScenarioResources, scripted: ScriptedUpstream
) -> None:
    harnesses = [runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX]
    models = await models_offered_for(client, harnesses)

    async def credentials(harness: runner_pb2.Harness) -> None:
        async with scenario_resources() as (owned_client, sandbox):
            await runner_credentials.vm_mtls_certificate_authenticates_a_session(
                cluster, owned_client, sandbox, harness, models[harness], scripted
            )

    async def turn(harness: runner_pb2.Harness, template: str) -> None:
        async with scenario_resources() as (owned_client, sandbox):
            await vm_sandbox.vm_sandbox_turn(owned_client, sandbox, harness, models[harness], scripted, template)

    async def oom() -> None:
        async with scenario_resources() as (owned_client, sandbox):
            await vm_sandbox.codex_tool_oom(owned_client, sandbox, models[runner_pb2.HARNESS_CODEX], scripted)

    # pytest-asyncio serializes separate tests. Launch every independent boot here so a
    # finished scenario never leaves its worker capacity idle behind another test's teardown.
    # Each task owns its guests and cleanup; the sampler has its own ownership scope.
    async with asyncio.TaskGroup() as scenarios:
        for harness in harnesses:
            scenarios.create_task(
                traced_scenario(f"runner-vm-mtls-{runner_pb2.Harness.Name(harness).lower()}", credentials(harness))
            )
        scenarios.create_task(
            traced_scenario(
                "central-sampler",
                central_sampler.dispatcher_central_setup_tool_and_retained_answer(
                    cluster,
                    tmp_path,
                    scripted,
                    Executor(template="runner-vm", cwd="/state/workspaces/accept"),
                    central_sampler.VM_EXECUTION,
                ),
            )
        )
        # Only Claude selects the private supervisor checks; both use the ordinary guest image.
        scenarios.create_task(
            traced_scenario("vm-turn-harness_claude", turn(runner_pb2.HARNESS_CLAUDE, SUPERVISION_TEMPLATE))
        )
        scenarios.create_task(traced_scenario("vm-turn-harness_codex", turn(runner_pb2.HARNESS_CODEX, VM_TEMPLATE)))
        scenarios.create_task(traced_scenario("codex-tool-oom", oom()))


if __name__ == "__main__":
    pytest_bazel.main()
