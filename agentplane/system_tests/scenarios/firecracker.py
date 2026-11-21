"""The deployed Firecracker backend preserves a live guest process through suspension."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import ssl
import tempfile
import textwrap
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import (
    Agent,
    environment_runtime_startup_retries,
    scripted_turn_with_admission_and_history,
)
from agentplane.system_tests.driver.app_sandbox import wait_until_running
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.sandbox import VM_WORKING_DIRECTORY, Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

_REPORT = "FIRECRACKER_PROOF "
_PROCESS_FILE = f"{VM_WORKING_DIRECTORY}/firecracker-process.json"
_CHILD = textwrap.dedent(
    """\
    import json, os, pathlib, time
    path = pathlib.Path(__import__('sys').argv[1])
    marker = os.urandom(24).hex()
    working_set = bytearray(64 * 1024 * 1024)
    working_set[::4096] = b'X' * (len(working_set) // 4096)
    (cgroup,) = [line.removeprefix('0::') for line in pathlib.Path('/proc/self/cgroup').read_text().splitlines()
                 if line.startswith('0::')]
    counter = 0
    while True:
        counter += 1
        evidence = dict(pid=os.getpid(), marker=marker, counter=counter, cgroup=cgroup,
                        start_ticks=pathlib.Path('/proc/self/stat').read_text().split()[21],
                        wall_ns=time.time_ns(), working_set_bytes=len(working_set))
        pending = path.with_suffix('.pending')
        pending.write_text(json.dumps(evidence))
        pending.replace(path)
        time.sleep(0.2)
    """
)


def _probe(*, start: bool, session_id: str, environment: tuple[str, str, str]) -> str:
    script = textwrap.dedent(
        f"""\
        import json, os, pathlib, subprocess, sys, time
        home = pathlib.Path.home()
        assert home == pathlib.Path('/state/home')
        assert home.stat().st_uid == os.getuid()
        assert home.stat().st_mode & 0o777 == 0o700
        (home / 'firecracker-home-proof').touch()
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
        rules = json.loads(subprocess.check_output(['agentplane', '--json', 'egress', 'rules'], text=True))
        evidence['egress_rules_identity'] = rules['identity']
        sessions = json.loads(subprocess.check_output(
            ['agentplane', '--json', 'session-dispatcher', 'list'], text=True))
        assert {session_id!r} in [session['sessionId'] for session in sessions['sessions']], sessions
        target = json.loads(subprocess.check_output(
            ['agentplane', '--json', 'environments', 'get', *{environment!r}], text=True))['target']
        assert (target['namespace'], target['name'], target['uid']) == {environment!r}, target
        evidence['mounts'] = {{line.split()[4]: line.split(' - ')[1].split()[0]
                              for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()}}
        print({_REPORT!r} + json.dumps(evidence))
        """
    )
    return f"python3 -c {shlex.quote(script)}"


async def _renew_credentials(cluster: Cluster, secret_name: str) -> tuple[str, str]:
    original = json.loads(await cluster.kubectl("get", "secret", secret_name, "-o", "json"))
    # cert-manager remains the issuer. Deleting this disposable test Secret
    # exercises real reissuance, kubelet projection and live Workload API updates.
    await cluster.kubectl("delete", "secret", secret_name)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(90), wait=wait_fixed(1), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            encoded = await cluster.kubectl("get", "secret", secret_name, "--ignore-not-found", "-o", "json")
            assert encoded, "cert-manager has not recreated the Secret"
            replacement = json.loads(encoded)
            assert replacement["metadata"]["uid"] != original["metadata"]["uid"]
            assert replacement["data"]["tls.crt"] != original["data"]["tls.crt"]
    return replacement["data"]["tls.crt"], replacement["data"]["ca.crt"]


async def _wait_for_served_certificate(cluster: Cluster, pod_name: str, credential: tuple[str, str]) -> None:
    proxy = json.loads(
        await cluster.kubectl("get", "secret", "environments-runtime-proxy-client-workload-tls", "-o", "json")
    )["data"]
    cert_pem, ca_pem = credential
    expected = x509.load_pem_x509_certificate(base64.b64decode(cert_pem))
    pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
    # Firecracker forwards inbound Pod-IP traffic to the guest. Kubernetes
    # port-forward connects to loopback inside the Pod and bypasses that DNAT.
    address = pod["status"]["podIP"]
    with tempfile.TemporaryDirectory() as directory:
        cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        cert.write_bytes(base64.b64decode(proxy["tls.crt"]))
        key.write_bytes(base64.b64decode(proxy["tls.key"]))
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # URI-SAN identity is checked against the exact expected certificate
        # below; chain verification remains enabled.
        trust.check_hostname = False
        trust.load_verify_locations(cadata=base64.b64decode(ca_pem).decode())
        trust.load_cert_chain(cert, key)
        trust.set_alpn_protocols(["h2"])
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(90),
            wait=wait_fixed(1),
            retry=retry_if_exception_type((AssertionError, OSError)),
            reraise=True,
        ):
            with attempt:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(address, 9090, ssl=trust, server_hostname="", ssl_handshake_timeout=5),
                    timeout=6,
                )
                actual = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
                # This probe ends at the authenticated TLS handshake. Abort instead
                # of racing TLS shutdown against the server's HTTP/2 SETTINGS frame.
                writer.transport.abort()
                await writer.wait_closed()
                assert actual == expected.public_bytes(Encoding.DER), "Runtime still serves its old certificate"


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
    agent = await Agent.create(
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
            shell_command=_probe(
                start=number == 1,
                session_id=str(agent.thread_id),
                environment=(view.target.namespace, view.target.name, view.target.uid),
            ),
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
        assert report["egress_rules_identity"] == {"name": view.identity_name}, report
        return report

    pod_json = json.loads(await cluster.kubectl("get", "pod", pod.name, "-o", "json"))
    (launcher,) = [
        container for container in pod_json["spec"]["containers"] if container["name"] == "environment-runtime"
    ]
    memory_limit = int(launcher["resources"]["limits"]["memory"])

    async def wait_for_checkpoint(cycle: int) -> dict[str, object]:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(300), wait=wait_fixed(1), retry=retry_if_exception_type(AssertionError), reraise=True
        ):
            with attempt:
                # Use the launcher's cgroup, which includes the VMM and buffered snapshot writes.
                # The kernel high-water mark captures spikes between samples.
                raw_memory = await cluster.kubectl(
                    "exec",
                    pod.name,
                    "-c",
                    "environment-runtime",
                    "--",
                    "sh",
                    "-ec",
                    'cg="/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)"; '
                    'cat "$cg/memory.current" "$cg/memory.peak" "$cg/memory.max" "$cg/memory.stat" "$cg/memory.events"',
                    timeout_s=10,
                )
                current, peak, maximum, *counters = raw_memory.splitlines()
                sample = {
                    "cycle": cycle,
                    "current_bytes": int(current),
                    "peak_bytes": int(peak),
                    "limit_bytes": int(maximum),
                    **{name: int(value) for name, value in (line.split() for line in counters)},
                }
                with (output / "launcher-memory.jsonl").open("a") as samples:
                    samples.write(json.dumps(sample) + "\n")
                assert sample["limit_bytes"] == memory_limit, sample
                assert sample["oom_kill"] == 0, sample
                suspended = await client.get_environment(view.target)
                assert suspended.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
                assert suspended.observed_state.stop_stage == "Stopped", suspended.observed_state
                assert suspended.kubernetes_diagnostics.pod.uid == pod.uid
        # The launcher reports suspended only after committing the checkpoint and reaping the VMM.
        status = json.loads(
            await cluster.kubectl(
                "get", "--raw", f"/api/v1/namespaces/{cluster.namespace}/pods/{pod.name}:9092/proxy/state"
            )
        )
        assert status["phase"] == "suspended", status
        return status

    before = await probe(1)
    assert before["pid1"] == "systemd"
    assert before["working_set_bytes"] == 64 * 1024 * 1024
    assert str(before["cgroup"]).startswith("/agentplane.slice/agentplane-commands.slice/"), before
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
    credential_id = pod_json["metadata"]["labels"]["agentplane.allegedly.works/environment-runtime-credential"]
    record = json.loads(await cluster.kubectl("get", "workloadcredential", view.target.name, "-o", "json"))
    assert record["status"]["credentialID"] == credential_id, record["status"]
    secret_name = record["status"]["secretName"]
    running_credential = await _renew_credentials(cluster, secret_name)
    await _wait_for_served_certificate(cluster, pod.name, running_credential)
    initial = await client.runtime_diagnostics(view.target)
    (process,) = [row for row in initial.processes if str(agent.thread_id) in row.assigned_session_ids]
    assert process.process_id
    assert initial.environment_runtime_boot_id
    # Exercise the conditional idle admission barrier as well as the VM primitive.
    # The live background child remains eligible for RAM-preserving autosuspend.
    await cluster.kubectl(
        "patch", "sandbox", view.target.name, "--type=merge", "-p", json.dumps({"spec": {"idleSuspendAfter": "1m"}})
    )
    status = await wait_for_checkpoint(1)
    stopped = json.loads(await cluster.kubectl("get", "sandbox", view.target.name, "-o", "json"))
    assert stopped["status"]["suspendedBy"] == "Idle", stopped["status"]
    assert stopped["status"]["firecrackerSuspension"]["phase"] == "Checkpointing", stopped["status"]
    pod_json = json.loads(await cluster.kubectl("get", "pod", pod.name, "-o", "json"))
    assert pod_json["metadata"]["uid"] == pod.uid
    assert all(row.get("restartCount", 0) == 0 for row in pod_json["status"]["containerStatuses"])
    thaw_credential = await _renew_credentials(cluster, secret_name)
    await cluster.kubectl(
        "patch", "sandbox", view.target.name, "--type=merge", "-p", json.dumps({"spec": {"idleSuspendAfter": None}})
    )
    await client.resume_sandbox(view.target)
    resumed = await wait_until_running(client, view.target)
    await _wait_for_served_certificate(cluster, pod.name, thaw_credential)
    assert resumed.kubernetes_diagnostics.pod.uid == pod.uid
    resumed_sandbox = json.loads(await cluster.kubectl("get", "sandbox", view.target.name, "-o", "json"))
    assert resumed_sandbox["status"]["firecrackerSuspension"]["phase"] == "Released", resumed_sandbox["status"]
    async for attempt in environment_runtime_startup_retries():
        with attempt:
            restored = await client.runtime_diagnostics(view.target)
    assert restored.environment_runtime_boot_id == initial.environment_runtime_boot_id
    (continued_process,) = [row for row in restored.processes if str(agent.thread_id) in row.assigned_session_ids]
    assert continued_process.process_id == process.process_id
    after = await probe(2)
    for key in ("boot_id", "pid", "start_ticks", "marker", "cgroup"):
        assert after[key] == before[key], (before, after)
    assert int(str(after["counter"])) > int(str(before["counter"]))

    # Exercise full capture from a restored, actively used memory mapping. A
    # cold capture alone never observes the overlapping restore and dump buffers.
    await client.suspend_sandbox(view.target)
    await wait_for_checkpoint(2)
    await client.resume_sandbox(view.target)
    resumed_again = await wait_until_running(client, view.target)
    assert resumed_again.kubernetes_diagnostics.pod.uid == pod.uid
    after_second = await probe(3)
    for key in ("boot_id", "pid", "start_ticks", "marker", "cgroup", "working_set_bytes"):
        assert after_second[key] == before[key], (before, after_second)
    assert int(str(after_second["counter"])) > int(str(after["counter"]))
    (output / "continuation.json").write_text(
        json.dumps(
            {
                "sandbox_uid": view.target.uid,
                "pod_uid": pod.uid,
                "runtime_boot_id": initial.environment_runtime_boot_id,
                "suspended_launcher": status,
                "process_id": process.process_id,
                "before": before,
                "after": after,
                "after_second": after_second,
            },
            indent=2,
        )
        + "\n"
    )
