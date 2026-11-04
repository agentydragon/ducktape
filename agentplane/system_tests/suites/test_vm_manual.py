"""Executable vm_manual scenarios sharing one k3s stack."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import storage, suspend_resume, vm_workspace_ownership
from agentplane.system_tests.vm import guest_logs
from agentplane.system_tests.vm.block_storage import STORAGE_CLASS

pytestmark = pytest.mark.vm_sandboxes


async def test_fresh_workspace_storage_survives_replacement(
    cluster: Cluster, client: Client, dispatcher: SessionDispatcherClient, executor: Executor
) -> None:
    await storage.fresh_workspace_storage_survives_replacement(
        cluster,
        client,
        dispatcher,
        executor,
        vm_storage=storage.VmStorage(storage_class=STORAGE_CLASS, check_boot=guest_logs.check_boot),
    )


@pytest.fixture
def executor() -> Executor:
    return Executor(template="environment-vm", cwd="/state/workspaces/accept")


@pytest.mark.scripted_model
async def test_two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    executor: Executor,
) -> None:
    await suspend_resume.two_harness_threads_keep_their_context_after_sandbox_suspend_resume(
        cluster, client, dispatcher, sandbox, scripted, executor
    )


@pytest.mark.vm_sandboxes
async def test_fresh_vm_session_workspace_is_created_for_harness_identity(
    client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes
) -> None:
    await vm_workspace_ownership.fresh_vm_session_workspace_is_created_for_harness_identity(client, dispatcher, sandbox)


if __name__ == "__main__":
    pytest_bazel.main()
