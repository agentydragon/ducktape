"""A VM Sandbox on the bare-k3s stack, its guest CPU emulated: it starts, suspends and resumes.

No model is involved. The guest is the Bazel-built test guest (guest/), not production's NixOS image.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.conftest import SANDBOX_READY_SECONDS, wait_until_running
from agentplane.app.sandbox_models import NewSandbox
from agentplane.app.testing.client import Client
from agentplane.sandbox_resource import OperatingMode
from agentplane.x.k3s_env import vm_sandboxes
from agentplane.x.k3s_env.stack import Stack, running_stack
from util.testing.undeclared_outputs import undeclared_outputs_dir

VM_TEMPLATE = "runner-vm"


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack(models=None) as running:
        try:
            yield running
        finally:
            (undeclared_outputs_dir() / "vm-sandboxes.txt").write_text(
                vm_sandboxes.report(running.k3s.kubectl, running.namespace)
            )


@pytest.fixture
async def client(stack: Stack) -> AsyncIterator[Client]:
    async with Client(base_url=stack.base_url, token=stack.token) as opened:
        yield opened


async def wait_until_suspended(client: Client, name: str) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(SANDBOX_READY_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert view.operating_mode is OperatingMode.SUSPENDED, f"{name} is {view.operating_mode}"
            assert view.pod is None, f"{name} still has its launcher: {view.pod}"


async def test_vm_sandbox_runs_suspends_and_resumes(client: Client) -> None:
    name = "vm-lifecycle"
    # Ready includes the bootstrap, which the runner runs in the guest.
    await client.create_sandbox(NewSandbox(name=name, template=VM_TEMPLATE, bootstrap="test -w /state/workspaces"))
    await wait_until_running(client, name)
    first_boot = (await client.runner_status(name)).runner_boot_id

    await client.suspend_sandbox(name)
    await wait_until_suspended(client, name)
    await client.resume_sandbox(name)
    await wait_until_running(client, name)
    assert (await client.runner_status(name)).runner_boot_id != first_boot


if __name__ == "__main__":
    pytest_bazel.main()
