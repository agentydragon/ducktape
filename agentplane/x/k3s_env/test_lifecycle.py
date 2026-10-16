"""Sandbox lifecycle flows against the bare-k3s stack: create, bootstrap, suspend, resume, delete.

No model is involved. One stack serves every test in the file; each test makes and removes its own Sandbox.
"""

import json
import os
import select
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus

import httpx
import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.conftest import POD_TEMPLATE, wait_until_running
from agentplane.app.sandbox_models import NewSandbox, SandboxView
from agentplane.app.testing.client import Client
from agentplane.sandbox_resource import OperatingMode
from agentplane.x.k3s_env import render
from agentplane.x.k3s_env.stack import Stack, image_ref, running_stack

TRANSITION_SECONDS = 300.0
STATE_FILE = "/state/lifecycle-marker"


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack(litellm_key=None) as running:
        yield running


async def wait_until_gone(client: Client, name: str) -> None:
    """Deletion is asynchronous: the Sandbox reads as Quiescing and Tearing down until it is removed."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            try:
                view = await client.sandbox(name)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != HTTPStatus.NOT_FOUND:
                    raise
                return
            raise AssertionError(f"{name} is still {view.phase}")


async def create_running(stack: Stack, client: Client, name: str, *, bootstrap: str = "") -> SandboxView:
    """Sandboxes are not removed afterwards: the stack is discarded with the action."""
    await client.create_sandbox(NewSandbox(name=name, template=POD_TEMPLATE, bootstrap=bootstrap))
    return await wait_until_running(client, name)


async def wait_until_suspended(client: Client, name: str) -> SandboxView:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert view.operating_mode is OperatingMode.SUSPENDED, f"{name} is {view.operating_mode}"
            assert view.pod is None, f"{name} still has its Pod: {view.pod}"
            return view
    raise AssertionError("unreachable: reraise=True either returns the view or raises")


def exec_in_sandbox(stack: Stack, view: SandboxView, script: str) -> str:
    assert view.pod is not None, f"{view.name} has no Pod"
    return stack.k3s.kubectl(
        "exec", "-n", render.NAMESPACE, f"pod/{view.pod.name}", "-c", "runner", "--", "sh", "-c", script, check=False
    )


async def wait_for_file(stack: Stack, client: Client, name: str, path: str) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert "present" in exec_in_sandbox(stack, view, f"test -f {path} && echo present"), f"{path} is absent"


@pytest.fixture
async def client(stack: Stack) -> AsyncIterator[Client]:
    async with Client(base_url=stack.base_url, token=stack.token) as opened:
        yield opened


async def test_create_reaches_running(stack: Stack, client: Client) -> None:
    view = await create_running(stack, client, "life-create")
    assert view.operating_mode is OperatingMode.RUNNING
    assert view.pod is not None
    assert int(exec_in_sandbox(stack, view, "id -u").strip()) > 0


def test_pod_image_volume_runs_bash_from_the_separate_tools_image(stack: Stack) -> None:
    """The v1.33 k3s/containerd fixture mounts the tools image and executes its Bash binary."""
    name = "layer-tools-volume"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": render.NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "runner",
                    "image": image_ref("runner"),
                    "imagePullPolicy": "Never",
                    "command": ["/bin/sh", "-c"],
                    "args": ["set -eu; /tools/usr/bin/bash -c 'printf POD_TOOLS_LAYER_OK'"],
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000},
                    "volumeMounts": [{"name": "tools", "mountPath": "/tools", "readOnly": True}],
                }
            ],
            "volumes": [
                {"name": "tools", "image": {"reference": image_ref("runner_tools"), "pullPolicy": "IfNotPresent"}}
            ],
        },
    }
    stack.k3s.kubectl("apply", "-f", "-", input_text=json.dumps(pod))
    watch = subprocess.Popen(
        [
            str(stack.k3s.binary),
            "kubectl",
            "--kubeconfig",
            str(stack.k3s.kubeconfig),
            "get",
            "pod",
            name,
            "-n",
            render.NAMESPACE,
            "--watch",
            "--output-watch-events",
            "-o",
            'jsonpath={.object.status.phase}{.status.phase}{"\\n"}',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert watch.stdout is not None
    deadline = time.monotonic() + 120
    phase: str | None = None
    pending = b""
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([watch.stdout], [], [], remaining)
            if not readable:
                break
            pending += os.read(watch.stdout.fileno(), 4096)
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                observed = line.decode().strip()
                if observed in {"Succeeded", "Failed"}:
                    phase = observed
                    break
            if phase is not None:
                break
    finally:
        watch.terminate()
        try:
            watch.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watch.kill()
            watch.wait()
    if phase != "Succeeded":
        details = stack.k3s.kubectl("describe", f"pod/{name}", "-n", render.NAMESPACE, check=False)
        stderr = watch.stderr.read().decode() if watch.stderr is not None else ""
        raise AssertionError(f"Pod with a separate tools image finished in phase {phase!r}: {stderr}\n{details}")
    logs = stack.k3s.kubectl("logs", f"pod/{name}", "-n", render.NAMESPACE)
    assert "POD_TOOLS_LAYER_OK" in logs, logs


async def test_bootstrap_script_runs_in_the_sandbox(stack: Stack, client: Client) -> None:
    await create_running(stack, client, "life-bootstrap", bootstrap="touch /state/bootstrapped")
    await wait_for_file(stack, client, "life-bootstrap", "/state/bootstrapped")


async def test_suspend_and_resume_keep_the_state_volume(stack: Stack, client: Client) -> None:
    name = "life-suspend"
    view = await create_running(stack, client, name)
    exec_in_sandbox(stack, view, f"touch {STATE_FILE}")
    await client.suspend_sandbox(name)
    await wait_until_suspended(client, name)
    await client.resume_sandbox(name)
    await wait_until_running(client, name)
    await wait_for_file(stack, client, name, STATE_FILE)


async def test_delete_removes_the_sandbox(stack: Stack, client: Client) -> None:
    name = "life-delete"
    await create_running(stack, client, name)
    await client.suspend_sandbox(name)
    await wait_until_suspended(client, name)
    await client.delete_sandbox(name)
    await wait_until_gone(client, name)


if __name__ == "__main__":
    pytest_bazel.main()
