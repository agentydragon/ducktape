"""Executable vm scenarios sharing one k3s stack."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.proto import session_pb2
from agentplane.system_tests.deployment.local_models import LOCAL_MODEL_SCENARIO_IDS
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE, sandbox_scope
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import traced_scenario
from agentplane.system_tests.scenarios import vm_sandbox
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.trace_contract import wait_for_vm_process_trace_contract
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# session_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_vm_scenarios(
    cluster: Cluster, native_platform: NativePlatformClients, phase_tracer: trace.Tracer, scripted: ScriptedUpstream
) -> None:
    async def turn(harness: session_pb2.Harness, template: str) -> None:
        async with sandbox_scope(cluster, native_platform.environment, phase_tracer) as sandbox:
            await vm_sandbox.vm_sandbox_turn(
                cluster, native_platform, sandbox, harness, LOCAL_MODEL_SCENARIO_IDS[harness], scripted, template
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

    await wait_for_vm_process_trace_contract(undeclared_outputs_dir())


if __name__ == "__main__":
    pytest_bazel.main()
