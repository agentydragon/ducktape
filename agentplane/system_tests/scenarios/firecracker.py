"""The deployed Firecracker backend preserves a live guest and native session through suspension."""

from __future__ import annotations

import json
import shlex
import textwrap
from uuid import uuid4

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.agent import (
    Agent,
    environment_runtime_startup_retries,
    scripted_turn_with_admission_and_history,
)
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.sandbox import VM_WORKING_DIRECTORY, Sandboxes, wait_until_running
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

_REPORT = "FIRECRACKER_PROOF "
_PROCESS_FILE = f"{VM_WORKING_DIRECTORY}/firecracker-process.json"
_CHILD = textwrap.dedent(
    """\
    import json, os, pathlib, time
    path = pathlib.Path(__import__('sys').argv[1])
    marker = os.urandom(24).hex()
    counter = 0
    while True:
        counter += 1
        evidence = dict(pid=os.getpid(), marker=marker, counter=counter,
                        start_ticks=pathlib.Path('/proc/self/stat').read_text().split()[21],
                        wall_ns=time.time_ns())
        pending = path.with_suffix('.pending')
        pending.write_text(json.dumps(evidence))
        pending.replace(path)
        time.sleep(0.2)
    """
)


def _probe(*, start: bool) -> str:
    script = textwrap.dedent(
        f"""\
        import json, os, pathlib, subprocess, sys, time
        path = pathlib.Path({_PROCESS_FILE!r})
        if {start!r}:
            subprocess.Popen([sys.executable, '-c', {_CHILD!r}, str(path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 10
        while not path.exists():
            assert time.monotonic() < deadline, 'live child did not start'
            time.sleep(0.05)
        first = json.loads(path.read_text())
        while True:
            evidence = json.loads(path.read_text())
            os.kill(evidence['pid'], 0)
            assert pathlib.Path(f"/proc/{{evidence['pid']}}/stat").read_text().split()[21] == evidence['start_ticks']
            if evidence['counter'] > first['counter']:
                break
            assert time.monotonic() < deadline, 'live child made no progress'
            time.sleep(0.05)
        evidence['boot_id'] = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        evidence['pid1'] = pathlib.Path('/proc/1/comm').read_text().strip()
        evidence['projected_sa_token_present'] = pathlib.Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()
        evidence['mounts'] = {{line.split()[4]: line.split(' - ')[1].split()[0]
                              for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()}}
        print({_REPORT!r} + json.dumps(evidence))
        """
    )
    return f"python3 -c {shlex.quote(script)}"


async def preserves_live_guest(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-firecracker-{uuid4().hex[:8]}", template="environment-firecracker")
    pod = view.kubernetes_diagnostics.pod
    assert pod.uid
    harness = session_pb2.HARNESS_CODEX
    offered = await models_offered_for(client, [harness])
    model = scripted.model(pod.name, harness)
    agent = await Agent.open(
        client,
        dispatcher,
        sandbox=view.target.name,
        sandbox_uid=view.target.uid,
        harness=harness,
        model=offered[harness],
        cwd=VM_WORKING_DIRECTORY,
    )
    output = undeclared_outputs_dir() / "firecracker"
    output.mkdir(exist_ok=True)

    async def probe(number: int) -> dict[str, object]:
        turn, _, _ = await scripted_turn_with_admission_and_history(
            client,
            agent,
            command_id=f"firecracker-{number}",
            prompt="Run the guest proof command.",
            model=model,
            history_turns=number,
            shell_command=_probe(start=number == 1),
            shell_call_id=f"guest-proof-{number}",
            response="Guest proof observed.",
        )
        (output / f"turn-{number}.txt").write_text(turn.transcript)
        reports = [
            line.removeprefix(_REPORT)
            for block in turn.tool_outputs
            for line in block.splitlines()
            if line.startswith(_REPORT)
        ]
        assert len(reports) == 1, turn.transcript
        report = json.loads(reports[0])
        assert isinstance(report, dict), report
        return report

    before = await probe(1)
    assert before["pid1"] == "systemd"
    assert before["projected_sa_token_present"] is False
    mounts = before["mounts"]
    assert isinstance(mounts, dict)
    for mount, filesystem in {
        "/": "ext4",
        "/run/agentplane/environment": "erofs",
        "/state": "ext4",
        "/state/workspaces": "ext4",
        "/run/agentplane/config": "iso9660",
        "/run/agentplane/sandbox": "iso9660",
        "/run/agentplane/trust": "iso9660",
    }.items():
        assert mounts[mount] == filesystem, before
    initial = await client.runner_status(view.target.name, view.target.uid)
    (session,) = [row for row in initial.sessions if row.session_id == str(agent.thread_id)]
    assert session.HasField("process_id")
    assert initial.environment_runtime_boot_id
    await client.suspend_sandbox(view.target)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(300), wait=wait_fixed(1), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            suspended = await client.get_environment(view.target)
            assert suspended.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
            assert suspended.observed_state.stop_stage == "Stopped", suspended.observed_state
            assert suspended.kubernetes_diagnostics.pod.uid == pod.uid
    # The launcher's suspended phase is published only after checkpoint commit and VMM reap.
    status = json.loads(
        await cluster.kubectl(
            "get", "--raw", f"/api/v1/namespaces/{cluster.namespace}/pods/{pod.name}:9092/proxy/state"
        )
    )
    assert status["phase"] == "suspended", status
    pod_json = json.loads(await cluster.kubectl("get", "pod", pod.name, "-o", "json"))
    assert pod_json["metadata"]["uid"] == pod.uid
    assert all(row.get("restartCount", 0) == 0 for row in pod_json["status"]["containerStatuses"])
    await client.resume_sandbox(view.target)
    resumed = await wait_until_running(client, view.target)
    assert resumed.kubernetes_diagnostics.pod.uid == pod.uid
    async for attempt in environment_runtime_startup_retries():
        with attempt:
            restored = await client.runner_status(view.target.name, view.target.uid)
    assert restored.environment_runtime_boot_id == initial.environment_runtime_boot_id
    (continuation,) = [row for row in restored.sessions if row.session_id == session.session_id]
    assert continuation.process_id == session.process_id
    assert continuation.native_session_id == session.native_session_id
    after = await probe(2)
    for key in ("boot_id", "pid", "start_ticks", "marker"):
        assert after[key] == before[key], (before, after)
    assert int(str(after["counter"])) > int(str(before["counter"]))
    (output / "continuation.json").write_text(
        json.dumps(
            {
                "sandbox_uid": view.target.uid,
                "pod_uid": pod.uid,
                "runtime_boot_id": initial.environment_runtime_boot_id,
                "suspended_launcher": status,
                "native_session_id": session.native_session_id,
                "process_id": session.process_id,
                "before": before,
                "after": after,
            },
            indent=2,
        )
        + "\n"
    )
