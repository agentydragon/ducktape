"""Real VM harness turns and Codex tool OOM containment, driven by the scripted model."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
import pytest_bazel
from opentelemetry import trace
from pydantic import BaseModel

from agentplane.app.testing.client import Client
from agentplane.native.codex import wire
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.proto import runner_pb2
from agentplane.system_tests.agent import TURN_SECONDS, Agent, Turn
from agentplane.system_tests.conftest import (
    VM_TEMPLATE,
    VM_WORKING_DIRECTORY,
    Sandboxes,
    models_offered_for,
    traced_vm_scenario,
)
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ShellCall, Text
from util.testing.undeclared_outputs import undeclared_outputs_dir

tracer = trace.get_tracer(__name__)

PING = "Run a shell command, then say exactly PING."

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
MEMORY_PROBE = """(set -eu
group=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)
snapshot=/run/agentplane-test/cgroups/${group##*/}.json
first=$(cat "$snapshot" 2>/dev/null || true)
for attempt in $(seq 1 50); do
    current=$(cat "$snapshot" 2>/dev/null || true)
    if [ -n "$current" ] && [ "$current" != "$first" ]; then
        printf 'VM_MEMORY %s\\n' "$current"
        exit 0
    fi
    sleep 0.1
done
echo "No fresh cgroup snapshot for $group" >&2
exit 1
)
"""

# Busybox awk is in the test guest. sprintf touches each allocation, so this is not merely virtual
# address reservation. Bound growth above the leaf's configured limit and prefer this command as
# the victim, making the tool-OOM path deterministic without changing the executor's OOM policy.
OOM_COMMAND = """set -eu
echo VM_OOM_ALLOCATING
echo 1000 > /proc/self/oom_score_adj
exec awk -v chunks="$((limit / 65536 + 1024))" 'BEGIN {
    for (i = 0; i < chunks; i++) blocks[i] = sprintf("%65536s", i)
    print "VM_OOM_UNEXPECTED_SURVIVAL"
    exit 42
}'
"""


class _MemoryEvidence(BaseModel):
    cgroup: str
    max: int
    swap_max: int
    oom_group: int
    executor_oom: int
    oom: int
    oom_kill: int
    oom_group_kill: int

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


async def _command(
    agent: Agent, model: CodexModel, *, call_id: str, command: str, exit_code: int
) -> tuple[Turn, _ExecResult]:
    with tracer.start_as_current_span("vm.command", attributes={"command.id": call_id}):
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


async def _run_vm_sandbox_turn(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    view = await sandbox(f"systest-vm-{harness}", template=VM_TEMPLATE)
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    with tracer.start_as_current_span("vm.session.open", attributes={"harness": runner_pb2.Harness.Name(harness)}):
        agent = await Agent.open(
            client, sandbox=view.name, harness=harness, model=offered_model, cwd=VM_WORKING_DIRECTORY
        )
    with tracer.start_as_current_span("vm.turn"):
        async with asyncio.TaskGroup() as turns:
            turn = turns.create_task(agent.run(PING))
            await model.reply(await model.request(), ShellCall("vm-supervision", VM_SUPERVISION_PROBE))
            result = await model.request()
            try:
                (output,) = result.tool_outputs
                assert output.call_id == "vm-supervision"
                assert output.contains("VM_UID 107"), output.blocks
                assert output.contains("VM_PID1 systemd"), output.blocks
                output_text = "\n".join(output.blocks)
                (cgroup_line,) = [line for line in output_text.splitlines() if line.startswith("VM_CGROUP ")]
                cgroup = cgroup_line.removeprefix("VM_CGROUP ")
                subtree = (
                    "agentplane-executors.slice"
                    if harness == runner_pb2.HARNESS_CODEX
                    else "agentplane-workloads.slice"
                )
                _assert_native_service_cgroup(cgroup, subtree)
                assert "VM_SYSTEMCTL_UNEXPECTED_SUCCESS" not in output_text, output.blocks
                systemctl_denial = re.search(
                    r"authentication|authorization|not authorized|access denied", output_text, re.IGNORECASE
                )
                assert systemctl_denial is not None, output.blocks
            finally:
                # Even a failed guest assertion should complete this scripted turn so endpoint
                # teardown reports the actual assertion, not a second unsettled exchange.
                await model.reply(result, Text("PING"))
        # The scripted reply is the thread's answer: the guest's model traffic came back through the launcher.
        assert "PING" in turn.result().answer, turn.result().transcript


async def _run_codex_tool_oom(
    client: Client, sandbox: Sandboxes, offered_model: str, scripted: ScriptedUpstream
) -> None:
    view = await sandbox("systest-vm-codex-oom", template=VM_TEMPLATE)
    assert view.pod is not None
    model = scripted.model(view.pod.name, runner_pb2.HARNESS_CODEX)
    assert isinstance(model, CodexModel)
    with tracer.start_as_current_span("vm.session.open", attributes={"harness": "HARNESS_CODEX"}):
        agent = await Agent.open(
            client, sandbox=view.name, harness=runner_pb2.HARNESS_CODEX, model=offered_model, cwd=VM_WORKING_DIRECTORY
        )
    evidence: dict[str, Any] = {}
    try:
        _, output = await _command(agent, model, call_id="memory-before", command=MEMORY_PROBE, exit_code=0)
        before = _MemoryEvidence.from_output(output.output)
        evidence["memory_before"] = before.model_dump()
        _assert_native_service_cgroup(before.cgroup, "agentplane-executors.slice")
        assert before.max > 0
        assert before.swap_max == before.oom_group == before.oom_group_kill == 0
        original = await client.runner_status(view.name)
        evidence["runner_before"] = original.model_dump(mode="json")
        (app_server,) = original.processes
        assert app_server.pid is not None
        assert app_server.state == "PROCESS_STATE_RUNNING"
        assert app_server.assigned_session_ids == [str(agent.thread_id)]
        (session,) = original.sessions
        assert session.process_id == app_server.process_id
        assert session.native_session_id is not None

        turn, output = await _command(
            agent, model, call_id="tool-oom", command=f"limit={before.max}\n" + OOM_COMMAND, exit_code=137
        )
        evidence["oom_tool_result"] = output.model_dump()
        evidence["oom_native_frames"] = turn.native
        assert "VM_OOM_ALLOCATING" in output.output
        assert "VM_OOM_UNEXPECTED_SURVIVAL" not in output.output
        (execution,) = frames.assert_item_lifecycles(
            [json.loads(line) for line in turn.native], wire.CommandExecutionItem
        )
        assert execution.status is wire.CommandExecutionStatus.FAILED
        assert execution.exit_code == 137

        # Inspect the Runner before a follow-up could lazily start a replacement harness.
        survived = await client.runner_status(view.name)
        evidence["runner_after_oom"] = survived.model_dump(mode="json")
        assert survived.runner_boot_id == original.runner_boot_id
        assert survived.processes == original.processes
        assert survived.sessions[0].native_session_id == session.native_session_id
        assert survived.sessions[0].execution_state == "SESSION_EXECUTION_STATE_READY"

        # A fresh turn on the same native Session must execute commands through the surviving leaf.
        followup, output = await _command(
            agent, model, call_id="memory-after", command=MEMORY_PROBE + "echo VM_OOM_FOLLOWUP_OK", exit_code=0
        )
        evidence["followup_tool_result"] = output.model_dump()
        after = _MemoryEvidence.from_output(output.output)
        evidence["memory_after"] = after.model_dump()
        assert "VM_OOM_FOLLOWUP_OK" in output.output
        assert after.cgroup == before.cgroup
        assert after.max == before.max
        # Either aggregate or leaf can hit its memory limit first. The aggregate's hierarchical
        # counter proves a memcg OOM in the executor subtree; the leaf counts its actual victims.
        assert after.executor_oom > before.executor_oom
        assert after.oom_kill == before.oom_kill + 1
        assert after.oom_group_kill == 0
        assert not followup.resumed
        (execution,) = frames.assert_item_lifecycles(
            [json.loads(line) for line in followup.native], wire.CommandExecutionItem
        )
        assert execution.status is wire.CommandExecutionStatus.COMPLETED
        assert execution.exit_code == 0
        final = await client.runner_status(view.name)
        evidence["runner_after_followup"] = final.model_dump(mode="json")
        assert final.runner_boot_id == original.runner_boot_id
        assert final.processes == original.processes
        assert final.sessions[0].native_session_id == session.native_session_id
        current = await client.sandbox(view.name)
        assert current.pod is not None
        assert current.pod.uid == view.pod.uid
        evidence["passed"] = True
    finally:
        (undeclared_outputs_dir() / "vm-codex-oom.json").write_text(json.dumps(evidence, indent=2) + "\n")


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
async def test_vm_sandbox_scenarios(client: Client, sandbox: Sandboxes, scripted: ScriptedUpstream) -> None:
    # pytest-asyncio runs parametrized cases sequentially. Schedule these explicitly so all three
    # independent VM boots overlap while sharing one k3s stack and Pod-routed scripted server.
    harnesses = [runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX]
    offered_models = await models_offered_for(client, harnesses)
    async with asyncio.TaskGroup() as scenarios:
        for harness in harnesses:
            name = f"vm-turn-{runner_pb2.Harness.Name(harness).lower()}"
            scenarios.create_task(
                traced_vm_scenario(
                    name, _run_vm_sandbox_turn(client, sandbox, harness, offered_models[harness], scripted)
                )
            )
        scenarios.create_task(
            traced_vm_scenario(
                "codex-tool-oom",
                _run_codex_tool_oom(client, sandbox, offered_models[runner_pb2.HARNESS_CODEX], scripted),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
