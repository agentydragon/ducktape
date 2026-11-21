"""Real VM harness turns and Codex tool OOM containment, driven by the scripted model."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.harness_host import harness_host_pb2
from agentplane.native.codex import wire
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.proto import identity_pb2, session_pb2
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import TURN_SECONDS, Agent, Turn, scripted_turn_with_admission_and_history
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import VM_WORKING_DIRECTORY, Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios import runner_credentials
from agentplane.system_tests.vm.configuration import OOM_TEMPLATE, SUPERVISION_TEMPLATE
from agentplane.system_tests.vm.guest_logs import check_boot, systemd_supervision_passed
from agentplane.system_tests.vm.host_qemu import observe_active_turn
from agentplane.system_tests.vm.supervision import session_setup_is_supervised
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

PING = "Run a shell command, then say exactly PING."
_COMMAND_SLICE = "/agentplane.slice/agentplane-commands.slice/"
_REPORT_CGROUP = """printf 'VM_CGROUP %s\\n' "$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)"\n"""
_EXECUTOR_PROBE = "/usr/sbin/agentplane-executor-probe"

VM_SUPERVISION_PROBE = r"""set -eu
printf 'VM_UID %s\n' "$(id -u)"
printf 'VM_PID1 %s\n' "$(cat /proc/1/comm)"
printf 'VM_CGROUP %s\n' "$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)"
if systemctl_output=$(/usr/bin/systemctl --no-ask-password restart agentplane-cgroup-observer.service 2>&1); then
    echo VM_SYSTEMCTL_UNEXPECTED_SUCCESS
    exit 23
fi
printf 'VM_SYSTEMCTL_OUTPUT %s\n' "$systemctl_output"
"""

# The test guest's root observer publishes kernel counters without granting the tool cgroup access.
# Wait for a new snapshot so the follow-up cannot accidentally read pre-OOM counters.
MEMORY_PROBE = (
    _REPORT_CGROUP
    + """(set -eu
snapshot=/run/agentplane-test/cgroups/commands.json
first=$(cat "$snapshot" 2>/dev/null || true)
for attempt in $(seq 1 50); do
    current=$(cat "$snapshot" 2>/dev/null || true)
    if [ -n "$current" ] && [ "$current" != "$first" ]; then
        printf 'VM_MEMORY %s\\n' "$current"
        exit 0
    fi
    sleep 0.1
done
echo "No fresh command slice snapshot" >&2
exit 1
)
"""
)

# Busybox awk is in the test guest. sprintf touches each allocation, so this is not merely virtual
# address reservation. Bound the allocation above the shared command slice limit.
OOM_COMMAND = (
    _REPORT_CGROUP
    + """set -eu
exec awk -v chunks="$((limit / 65536 + 1024))" 'BEGIN {
    for (i = 0; i < chunks; i++) blocks[i] = sprintf("%65536s", i)
    exit 42
}'
"""
)


class _MemoryEvidence(BaseModel):
    executor_cgroup: str
    executor_pid: int
    executor_start_ticks: int
    executor_oom_kill: int
    max: int
    swap_max: int
    oom: int
    oom_kill: int

    @classmethod
    def from_output(cls, output: str) -> _MemoryEvidence:
        (report,) = [line.removeprefix("VM_MEMORY ") for line in output.splitlines() if line.startswith("VM_MEMORY ")]
        return cls.model_validate_json(report)


class _ExecResult(BaseModel):
    output: str
    exit_code: int


def _assert_native_service_cgroup(cgroup: str, slice_name: str) -> None:
    prefix = f"/agentplane.slice/{slice_name}/agentplane-native-"
    assert cgroup.startswith(prefix), cgroup
    unit = cgroup.rsplit("/", 1)[-1]
    assert re.fullmatch(
        r"agentplane-native-.+-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.service", unit
    ), cgroup


def _command_cgroup(output: str) -> str:
    (cgroup,) = [line.removeprefix("VM_CGROUP ") for line in output.splitlines() if line.startswith("VM_CGROUP ")]
    assert cgroup.startswith(_COMMAND_SLICE), cgroup
    return cgroup


def _execution_session(output: str) -> str:
    (session_id,) = [
        line.removeprefix("VM_EXECUTION_SESSION ")
        for line in output.splitlines()
        if line.startswith("VM_EXECUTION_SESSION ")
    ]
    assert session_id, output
    return session_id


async def _command(
    agent: Agent, model: CodexModel, *, call_id: str, command: str, exit_code: int
) -> tuple[Turn, _ExecResult]:
    with tracer.start_as_current_span("system_test.command", attributes={"command.id": call_id}):
        async with asyncio.timeout(TURN_SECONDS), asyncio.TaskGroup() as turns:
            turn = turns.create_task(agent.run("Run the requested shell command and report its result."))
            request = await model.request()
            # Emit the nested result, including its exit status, through the actual code-mode tool.
            script = (
                '// @exec: {"yield_time_ms": 1}\n'
                f"let result = await tools.exec_command({json.dumps({'cmd': command, 'yield_time_ms': 1000})});\n"
                "while (result.session_id != null) {\n"
                "  const next = await tools.write_stdin({session_id: result.session_id, chars: '', yield_time_ms: 1000});\n"
                "  result = {...next, output: result.output + next.output};\n"
                "}\n"
                "text('VM_EXEC_RESULT ' + JSON.stringify(result));"
            )
            async with request._exchange as exchange:
                assert request.native.code_mode
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.CustomToolCall(call_id, "exec", script)], model=model.model
                    ).events
                )
            output_call_id = call_id
            blocks: list[str] = []
            waits = 0
            while True:
                result = await model.request()
                (output,) = [output for output in result.tool_outputs if output.call_id == output_call_id]
                blocks.extend(output.blocks)
                running = re.search(r"^Script running with cell ID (\S+)", "\n".join(output.blocks), re.MULTILINE)
                if running is None:
                    break
                waits += 1
                output_call_id = f"{call_id}-wait-{waits}"
                async with result._exchange as exchange:
                    await exchange.send(
                        *responses_sse.response_stream(
                            [
                                responses_sse.FunctionCall(
                                    output_call_id, "wait", {"cell_id": running[1], "yield_time_ms": 1000}
                                )
                            ],
                            model=model.model,
                        ).events
                    )
            await model.reply(result, Text("Command observed."))
        reports = [block.removeprefix("VM_EXEC_RESULT ") for block in blocks if block.startswith("VM_EXEC_RESULT ")]
        assert len(reports) == 1, blocks
        executed = _ExecResult.model_validate_json(reports[0])
        assert executed.exit_code == exit_code, output
        return turn.result(), executed


async def vm_sandbox_turn(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    template: str,
) -> None:
    view = await sandbox(f"systest-vm-{harness}", template=template)
    assert view.kubernetes_diagnostics.HasField("pod")
    identity = await runner_credentials.check_vm_runner_certificate(cluster, view)
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
            client, dispatcher, view.target.name, view.target.uid, harness, offered_model
        )
    with (
        tracer.start_as_current_span("system_test.turn"),
        observe_active_turn(undeclared_outputs_dir() / "vm-artifacts", view.target.name, view.target.uid),
    ):
        turn, _, _ = await scripted_turn_with_admission_and_history(
            client,
            agent,
            model=model,
            command_id=f"vm-{view.target.uid}",
            prompt=PING,
            shell_command=VM_SUPERVISION_PROBE,
            shell_call_id="vm-supervision",
            response="PING",
        )
    assert len(turn.tool_outputs) == 1, turn.transcript
    output_text = turn.tool_outputs[0]
    assert "VM_UID 107" in output_text, turn.transcript
    assert "VM_PID1 systemd" in output_text, turn.transcript
    (cgroup_line,) = [line for line in output_text.splitlines() if line.startswith("VM_CGROUP ")]
    cgroup = cgroup_line.removeprefix("VM_CGROUP ")
    if harness == session_pb2.HARNESS_CODEX:
        assert cgroup.startswith(_COMMAND_SLICE), cgroup
    else:
        _assert_native_service_cgroup(cgroup, "agentplane-workloads.slice")
    assert "VM_SYSTEMCTL_UNEXPECTED_SUCCESS" not in output_text, turn.transcript
    systemctl_denial = re.search(
        r"authentication|authorization|not authorized|access denied", output_text, re.IGNORECASE
    )
    assert systemctl_denial is not None, turn.transcript
    assert "PING" in turn.answer, turn.transcript
    rules_turn = await agent.run_shell(model, "agentplane --json egress rules")
    rules_output = rules_turn.tool_outputs[-1]
    start = rules_output.find("{")
    assert start >= 0, rules_turn.transcript
    rules, _ = json.JSONDecoder().raw_decode(rules_output[start:])
    assert rules["identity"] == {"name": view.identity_name}, rules_turn.transcript
    platform_turn = await agent.run_shell(
        model,
        "agentplane notifications subscriptions --limit 1 >/dev/null && echo NOTIFICATIONS_GRPC_OK && "
        "agentplane session-dispatcher list --limit 1 >/dev/null && echo SESSIONS_GRPC_OK && "
        "agentplane environments list --limit 1 >/dev/null && echo ENVIRONMENTS_GRPC_OK",
    )
    platform_output = platform_turn.tool_outputs[-1]
    assert "NOTIFICATIONS_GRPC_OK" in platform_output, platform_turn.transcript
    assert "SESSIONS_GRPC_OK" in platform_output, platform_turn.transcript
    assert "ENVIRONMENTS_GRPC_OK" in platform_output, platform_turn.transcript
    await runner_credentials.assert_vm_runner_identity_unchanged(
        cluster, view.kubernetes_diagnostics.pod.name, identity
    )


async def _capture_native_profile(cluster: Cluster, sandbox_name: str, sandbox_uid: str) -> Path:
    """Copy the selected guest's perf.data from its local-path state PVC before Sandbox teardown."""
    claim = json.loads(await cluster.kubectl("get", "pvc", f"{sandbox_name}-state", "-o", "json"))
    assert claim["metadata"]["labels"]["agentplane.allegedly.works/sandbox"] == sandbox_name
    assert any(
        owner["kind"] == "Sandbox" and owner["name"] == sandbox_name and owner["uid"] == sandbox_uid
        for owner in claim["metadata"]["ownerReferences"]
    )
    volume = json.loads(await cluster.kubectl("get", "pv", claim["spec"]["volumeName"], "-o", "json"))
    assert volume["spec"]["claimRef"]["uid"] == claim["metadata"]["uid"]
    assert volume["spec"]["claimRef"]["namespace"] == cluster.namespace
    assert volume["spec"]["claimRef"]["name"] == claim["metadata"]["name"]
    source = Path((volume["spec"].get("hostPath") or volume["spec"]["local"])["path"])
    output = undeclared_outputs_dir() / "vm-artifacts" / "profiles" / f"{sandbox_uid}.perf.data"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "testing" / "profiles" / "native-startup.perf.data", output)
    assert output.stat().st_size > 1024
    with output.open("rb") as profile:
        assert profile.read(8) == b"PERFILE2"
    return output


async def codex_tool_oom(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    control: SandboxControlClient,
    sandbox: Sandboxes,
    offered_model: str,
    scripted: ScriptedUpstream,
    *,
    native_profile: bool,
) -> None:
    view = await sandbox("systest-vm-codex-oom", template=OOM_TEMPLATE)
    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, session_pb2.HARNESS_CODEX)
    assert isinstance(model, CodexModel)
    evidence: dict[str, Any] = {}
    try:
        with tracer.start_as_current_span(
            "system_test.session.create", attributes={"harness": "HARNESS_CODEX", "sandbox.name": view.target.name}
        ):
            agent = await Agent.create(
                client,
                dispatcher,
                sandbox=view.target.name,
                sandbox_uid=view.target.uid,
                harness=session_pb2.HARNESS_CODEX,
                model=offered_model,
                cwd=VM_WORKING_DIRECTORY,
            )
        with tracer.start_as_current_span(
            "system_test.vm_oom.baseline", record_exception=False, set_status_on_exception=False
        ):
            _, output = await _command(
                agent, model, call_id="memory-before", command="set -e\n" + MEMORY_PROBE + _EXECUTOR_PROBE, exit_code=0
            )
            before = _MemoryEvidence.from_output(output.output)
            native_session = _execution_session(output.output)
            evidence["native_session"] = native_session
            evidence["command_cgroup_before"] = _command_cgroup(output.output)
            evidence["memory_before"] = before.model_dump()
            _assert_native_service_cgroup(before.executor_cgroup, "agentplane-executors.slice")
            assert before.executor_pid > 0
            assert before.executor_start_ticks > 0
            assert before.max > 0
            assert before.swap_max == 0
            original = await client.runtime_diagnostics(view.target)
            evidence["runner_before"] = MessageToDict(
                original, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
            )
        (app_server,) = original.processes
        assert app_server.HasField("pid")
        assert app_server.state == session_pb2.PROCESS_STATE_RUNNING
        assert list(app_server.assigned_session_ids) == [str(agent.thread_id)]
        (session,) = await _host_session(control, view)
        assert session.environment_hosted.harness.state == harness_host_pb2.HarnessAttachment.STATE_ATTACHED

        with tracer.start_as_current_span(
            "system_test.vm_oom.trigger_and_survival", record_exception=False, set_status_on_exception=False
        ):
            turn, output = await _command(
                agent, model, call_id="tool-oom", command=f"limit={before.max}\n" + OOM_COMMAND, exit_code=137
            )
            evidence["oom_tool_result"] = output.model_dump()
            evidence["oom_native_frames"] = turn.native
            evidence["command_cgroup_oom"] = _command_cgroup(output.output)
            (execution,) = frames.assert_item_lifecycles(
                [json.loads(line) for line in turn.native], wire.CommandExecutionItem
            )
            assert execution.status is wire.CommandExecutionStatus.FAILED
            assert execution.exit_code == 137

            # Inspect the Runner before a follow-up could lazily start a replacement harness.
            survived = await client.runtime_diagnostics(view.target)
            evidence["runner_after_oom"] = MessageToDict(
                survived, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
            )
            assert survived.environment_runtime_boot_id == original.environment_runtime_boot_id
            assert list(survived.processes) == list(original.processes)
            (survived_session,) = await _host_session(control, view)
            assert survived_session == session

        # A fresh turn on the same Session must execute through the original executor instance.
        with tracer.start_as_current_span(
            "system_test.vm_oom.same_session_recovery", record_exception=False, set_status_on_exception=False
        ):
            followup, output = await _command(
                agent,
                model,
                call_id="memory-after",
                command="set -e\n" + MEMORY_PROBE + f"{_EXECUTOR_PROBE} {shlex.quote(native_session)}",
                exit_code=0,
            )
            evidence["followup_tool_result"] = output.model_dump()
            evidence["command_cgroup_after"] = _command_cgroup(output.output)
            assert _execution_session(output.output) == native_session
            after = _MemoryEvidence.from_output(output.output)
            evidence["memory_after"] = after.model_dump()
            assert after.executor_cgroup == before.executor_cgroup
            assert after.executor_pid == before.executor_pid
            assert after.executor_start_ticks == before.executor_start_ticks
            assert after.executor_oom_kill == before.executor_oom_kill
            assert after.max == before.max
            assert after.oom > before.oom
            assert after.oom_kill > before.oom_kill
            assert not followup.resumed
            (execution,) = frames.assert_item_lifecycles(
                [json.loads(line) for line in followup.native], wire.CommandExecutionItem
            )
            assert execution.status is wire.CommandExecutionStatus.COMPLETED
            assert execution.exit_code == 0
            final = await client.runtime_diagnostics(view.target)
            evidence["runner_after_followup"] = MessageToDict(
                final, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
            )
            assert final.environment_runtime_boot_id == original.environment_runtime_boot_id
            assert list(final.processes) == list(original.processes)
            (final_session,) = await _host_session(control, view)
            assert final_session == session
            current = await client.get_environment(view.target)
            assert current.kubernetes_diagnostics.HasField("pod")
            assert current.kubernetes_diagnostics.pod.uid == view.kubernetes_diagnostics.pod.uid
            evidence["passed"] = True
    finally:
        scenario_failed = sys.exc_info()[0] is not None
        try:
            if native_profile:
                evidence["native_profile"] = str(
                    await _capture_native_profile(cluster, view.target.name, str(view.target.uid))
                )
        except Exception as error:
            evidence["native_profile_error"] = repr(error)
            if not scenario_failed:
                raise
        finally:
            (undeclared_outputs_dir() / "vm-codex-oom.json").write_text(json.dumps(evidence, indent=2) + "\n")


async def _host_session(
    control: SandboxControlClient, view: environment_service_pb2.EnvironmentDescriptor
) -> list[harness_host_pb2.Session]:
    assert view.kubernetes_diagnostics.HasField("pod")
    target = view.target
    destination = identity_pb2.SandboxDestination(
        owner=identity_pb2.ServiceAccount(namespace=target.namespace, name=target.name), target=target
    )
    return await control.runner(destination).list_sessions(expected_incarnation_uid=view.kubernetes_diagnostics.pod.uid)


# gazelle:include_dep @pypi//protobuf
