"""Real VM harness turns driven by the scripted model."""

from __future__ import annotations

import json

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.proto import session_pb2
from agentplane.system_tests.driver import workload_credentials
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.native_session import native_scripted_turn_with_admission_and_history
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.vm.configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.guest_logs import check_boot, systemd_supervision_passed
from agentplane.system_tests.vm.guest_supervision_probe import VM_SUPERVISION_PROBE, assert_vm_supervision_probe
from agentplane.system_tests.vm.host_qemu import observe_active_turn
from agentplane.system_tests.vm.supervision import session_setup_is_supervised
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

PING = "Run a shell command, then say exactly PING."


async def vm_sandbox_turn(
    cluster: Cluster,
    platform: NativePlatformClients,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    template: str,
) -> None:
    view = await sandbox(f"systest-vm-{harness}", template=template)
    await platform.ready_runner(view.target)
    assert view.kubernetes_diagnostics.HasField("pod")
    identity = await workload_credentials.check_vm_runner_certificate(cluster, view)
    if template == SUPERVISION_TEMPLATE:
        async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                check_boot()
                assert systemd_supervision_passed(view.kubernetes_diagnostics.pod.uid), (
                    "Missing this VM's supervisor check receipt"
                )
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    with tracer.start_as_current_span(
        "system_test.session.create",
        attributes={"harness": session_pb2.Harness.Name(harness), "sandbox.name": view.target.name},
    ):
        agent = await session_setup_is_supervised(
            platform.history, platform.dispatcher, view.target.name, view.target.uid, harness, offered_model
        )
    with (
        tracer.start_as_current_span("system_test.turn"),
        observe_active_turn(undeclared_outputs_dir() / "vm-artifacts", view.target.name, view.target.uid),
    ):
        turn, _, _ = await native_scripted_turn_with_admission_and_history(
            platform.history,
            agent,
            view.target,
            model,
            command_id=f"vm-{view.target.uid}",
            prompt=PING,
            shell_command=VM_SUPERVISION_PROBE,
            shell_call_id="vm-supervision",
            response="PING",
        )
    assert len(turn.tool_outputs) == 1, turn.transcript
    output_text = turn.tool_outputs[0]
    subtree = "agentplane-executors.slice" if harness == session_pb2.HARNESS_CODEX else "agentplane-workloads.slice"
    assert_vm_supervision_probe(output_text, slice_name=subtree)
    assert "PING" in turn.answer, turn.transcript
    rules_turn = await agent.run_shell(model, "agentplane --json egress rules")
    rules_output = rules_turn.tool_outputs[-1]
    start = rules_output.find("{")
    assert start >= 0, rules_turn.transcript
    rules, _ = json.JSONDecoder().raw_decode(rules_output[start:])
    assert rules["identity"] == {"name": view.identity_name}, rules_turn.transcript
    notifications_turn = await agent.run_shell(
        model, "agentplane notifications subscriptions --limit 1 >/dev/null && echo NOTIFICATIONS_GRPC_OK"
    )
    assert "NOTIFICATIONS_GRPC_OK" in notifications_turn.tool_outputs[-1], notifications_turn.transcript
    await workload_credentials.assert_vm_runner_identity_unchanged(
        cluster, view.kubernetes_diagnostics.pod.name, identity
    )


# gazelle:include_dep @pypi//protobuf
