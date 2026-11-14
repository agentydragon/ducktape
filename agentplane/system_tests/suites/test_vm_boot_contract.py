"""Deployed VM boots, issued credentials, workspace access and platform CLI routes."""

from __future__ import annotations

import asyncio
import json
import shlex

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.sandbox import VM_TEMPLATE
from agentplane.system_tests.driver.scenario import ScenarioResources
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import traced_scenario, tracer
from agentplane.system_tests.scenarios import runner_credentials
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.guest_logs import check_boot, systemd_supervision_passed
from agentplane.system_tests.vm.supervision import session_setup_is_supervised
from agentplane.system_tests.vm.trace_contract import wait_for_vm_process_trace_contract
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# session_pb2.pyi imports protobuf stubs directly during mypy's dependency traversal.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.vm_sandboxes


async def _wait_for_private_supervision_receipt(pod_uid: str) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            check_boot()
            assert systemd_supervision_passed(pod_uid), "Missing this VM's supervisor check receipt"


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_vm_boot_contract(
    client: Client,
    cluster: Cluster,
    dispatcher: SessionDispatcherClient,
    scenario_resources: ScenarioResources,
    scripted: ScriptedUpstream,
) -> None:
    harnesses = [session_pb2.HARNESS_CLAUDE, session_pb2.HARNESS_CODEX]
    models = await models_offered_for(client, harnesses)

    async def check_guest(harness: session_pb2.Harness, template: str) -> None:
        harness_name = session_pb2.Harness.Name(harness)
        async with scenario_resources() as (guest_client, sandbox):
            suffix = harness_name.removeprefix("HARNESS_").lower()
            view = await sandbox(f"systest-vm-boot-candidate-{suffix}", template=template)

            assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
            assert view.observed_state.phase == environment_service_pb2.SANDBOX_PHASE_RUNNING
            assert view.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
            assert view.kubernetes_diagnostics.HasField("pod")
            assert view.kubernetes_diagnostics.pod.uid

            identity = await runner_credentials.check_vm_runner_certificate(cluster, view)
            assert identity[0] == view.kubernetes_diagnostics.pod.uid
            assert identity[1]

            # This typed public app endpoint calls GetStatus through Runner Gateway to the VM Runtime,
            # proving both services are reachable over their authenticated transport after image boot.
            runtime = await guest_client.runner_status(view.target.name, view.target.uid)
            assert runtime.environment_runtime_boot_id
            assert runtime.HasField("lifecycle")
            assert runtime.lifecycle.desired_mode == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
            assert runtime.lifecycle.progress_available
            assert runtime.lifecycle.progress_current
            assert runtime.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
            assert not runtime.lifecycle.error
            assert not runtime.lifecycle.HasField("blocker")

            if template == SUPERVISION_TEMPLATE:
                await _wait_for_private_supervision_receipt(view.kubernetes_diagnostics.pod.uid)

            model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
            with tracer.start_as_current_span(
                "system_test.session.create", attributes={"harness": harness_name, "sandbox.name": view.target.name}
            ):
                agent = await session_setup_is_supervised(
                    guest_client, dispatcher, view.target.name, view.target.uid, harness, models[harness]
                )
            marker = f"VM_WORKSPACE_ROUNDTRIP_{harness_name}_{view.target.uid.replace('-', '')}"
            workspace_file = f".agentplane-vm-boot-candidate-{view.target.uid.replace('-', '')}"
            command = "\n".join(
                [
                    "set -eu",
                    "test -w .",
                    f"printf '%s\\n' {shlex.quote(marker)} > {shlex.quote(workspace_file)}",
                    f'test "$(cat {shlex.quote(workspace_file)})" = {shlex.quote(marker)}',
                    f"cat {shlex.quote(workspace_file)}",
                    f"rm -- {shlex.quote(workspace_file)}",
                    f"test ! -e {shlex.quote(workspace_file)}",
                    "printf '\\nVM_EGRESS_RULES_JSON\\n'",
                    "agentplane --json egress rules",
                    "printf '\\nVM_EGRESS_RULES_END\\n'",
                    "agentplane notifications subscriptions --limit 1 >/dev/null",
                    "echo NOTIFICATIONS_GRPC_OK",
                ]
            )
            with tracer.start_as_current_span("system_test.turn"):
                turn = await agent.run_shell(model, command)
            assert marker in turn.tool_outputs[-1], turn.transcript
            rules_output = (
                turn.tool_outputs[-1].split("VM_EGRESS_RULES_JSON\n", 1)[1].split("VM_EGRESS_RULES_END\n", 1)[0]
            )
            rules = json.loads(rules_output)
            assert rules["identity"] == {"name": view.identity_name}, turn.transcript
            assert "NOTIFICATIONS_GRPC_OK" in turn.tool_outputs[-1], turn.transcript

            await runner_credentials.assert_vm_runner_identity_unchanged(
                cluster, view.kubernetes_diagnostics.pod.name, identity
            )

    async with asyncio.TaskGroup() as guests:
        guests.create_task(
            traced_scenario("vm-turn-harness_claude", check_guest(session_pb2.HARNESS_CLAUDE, SUPERVISION_TEMPLATE))
        )
        guests.create_task(
            traced_scenario("vm-turn-harness_codex", check_guest(session_pb2.HARNESS_CODEX, VM_TEMPLATE))
        )

    await wait_for_vm_process_trace_contract(undeclared_outputs_dir())


if __name__ == "__main__":
    pytest_bazel.main()
