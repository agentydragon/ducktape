"""Refresh a synthetic key in running Pod and VM Sandboxes through their API identity."""

from __future__ import annotations

import asyncio
import json
import shlex
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP
from agentplane.runner.harness import Harness
from agentplane.system_tests import runner_key
from agentplane.system_tests.cluster import kubectl
from agentplane.system_tests.conftest import POD_TEMPLATE, VM_TEMPLATE, Sandboxes, namespace
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def _identity(name: str, *, vm: bool) -> dict:
    sandbox = json.loads(await kubectl("get", f"sandboxes.{GROUP}", name, "-o", "json"))
    pods = json.loads(await kubectl("get", "pods", "-l", f"agentplane.allegedly.works/sandbox={name}", "-o", "json"))
    identity = {
        "sandbox_uid": sandbox["metadata"]["uid"],
        "incarnation": sandbox["status"]["incarnation"],
        "pods": [
            {
                "uid": pod["metadata"]["uid"],
                "restarts": {
                    container["name"]: container["restartCount"] for container in pod["status"]["containerStatuses"]
                },
            }
            for pod in pods["items"]
        ],
    }
    if vm:
        vmi = json.loads(await kubectl("get", "virtualmachineinstance", name, "-o", "json"))
        identity["vmi_uid"] = vmi["metadata"]["uid"]
    return identity


async def _probe(client: Client, name: str, *, expected: str, vm: bool) -> str:
    models = (await client.models()).harnesses[Harness.CODEX]
    assert models
    session = uuid4()
    source = f"""set -eu
[ "$(id -u)" = {107 if vm else 1000} ]
[ "${runner_key.ENV_VAR}" = {shlex.quote(runner_key.MOUNT_PATH + "/" + runner_key.FILE_NAME)} ]
for attempt in $(seq 1 90); do
    if [ "$(cat "${runner_key.ENV_VAR}")" = {shlex.quote(expected)} ]; then
        [ "$(stat -c %a "${runner_key.ENV_VAR}")" = 440 ]
        if kubectl -n {shlex.quote(namespace())} get secret {runner_key.UNRELATED_SECRET_NAME} -o name >/tmp/key-denial 2>&1; then
            echo 'unrelated Secret was readable' >&2
            exit 1
        fi
        grep -qi forbidden /tmp/key-denial
        echo KEY_PROBE_OK
        exit 0
    fi
    sleep 1
done
echo 'key refresh did not arrive' >&2
exit 1
"""
    await client.create_session(
        name,
        session,
        overrides={"harness": "HARNESS_CODEX", "model": models[0], "cwd": "/state/workspaces/accept"},
        setup_script=source,
    )
    await client.wait_for_thread_projection(session, timeout_s=120)
    stdout, stderr = bytearray(), bytearray()
    async with asyncio.timeout(120):
        async for entry in client.events(session, after=0, read_seconds=120):
            if entry.event.HasField("setup_output"):
                stdout.extend(entry.event.setup_output.stdout)
                stderr.extend(entry.event.setup_output.stderr)
            elif entry.event.HasField("setup_finished"):
                assert entry.event.setup_finished.exit_code == 0, stderr.decode()
                assert "KEY_PROBE_OK" in stdout.decode(), stdout.decode()
                return str(session)
            if entry.event.HasField("setup_interrupted"):
                if not vm:
                    (undeclared_outputs_dir() / "runner-setup.log").write_text(
                        await kubectl("logs", name, "-c", "runner", "--tail=100")
                    )
                raise AssertionError("key probe setup was interrupted")
    raise AssertionError("key probe emitted no setup result")


@pytest.mark.parametrize("vm", [False, pytest.param(True, marks=pytest.mark.vm_sandboxes)])
async def test_running_sandbox_observes_key_rotation(client: Client, sandbox: Sandboxes, vm: bool) -> None:
    view = await sandbox(f"systest-key-{uuid4().hex[:10]}", template=VM_TEMPLATE if vm else POD_TEMPLATE)
    first = await _probe(client, view.name, expected=runner_key.INITIAL_VALUE, vm=vm)
    before = await _identity(view.name, vm=vm)
    await kubectl(
        "patch",
        "secret",
        runner_key.SECRET_NAME,
        "--type=merge",
        "-p",
        json.dumps({"stringData": {runner_key.FILE_NAME: runner_key.ROTATED_VALUE}}),
    )
    second = await _probe(client, view.name, expected=runner_key.ROTATED_VALUE, vm=vm)
    after = await _identity(view.name, vm=vm)
    assert before == after
    (undeclared_outputs_dir() / "runner-key-rotation.json").write_text(
        json.dumps(
            {
                "vm": vm,
                "identity_before": before,
                "identity_after": after,
                "sessions": [first, second],
                "rotation_observed": True,
                "unrelated_secret_denied": True,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    pytest_bazel.main()
