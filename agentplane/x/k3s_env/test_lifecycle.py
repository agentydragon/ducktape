"""Sandbox lifecycle flows against the bare-k3s stack: create, bootstrap, suspend, resume, delete.

No model is involved. One stack serves every test in the file; each test makes and removes its own Sandbox.
"""

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
from agentplane.x.k3s_env.stack import Stack, running_stack

TRANSITION_SECONDS = 300.0
STATE_FILE = "/state/lifecycle-marker"


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack(litellm_key=None) as running:
        yield running


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
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(TRANSITION_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            with pytest.raises(httpx.HTTPStatusError) as gone:
                await client.sandbox(name)
            assert gone.value.response.status_code == HTTPStatus.NOT_FOUND


if __name__ == "__main__":
    pytest_bazel.main()
