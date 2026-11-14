"""Real-cluster controls for storage's asynchronous controller precondition."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.scenarios import storage


@asynccontextmanager
async def _stopped_controller(cluster: Cluster) -> AsyncIterator[None]:
    deployment = json.loads(await cluster.kubectl("get", "deployment/sandbox-controller", "-o", "json"))
    assert deployment["spec"]["replicas"] == 1
    selector = ",".join(f"{key}={value}" for key, value in deployment["spec"]["selector"]["matchLabels"].items())
    pods = json.loads(await cluster.kubectl("get", "pods", f"--selector={selector}", "-o", "json"))["items"]
    try:
        await cluster.kubectl("scale", "deployment/sandbox-controller", "--replicas=0")
        if pods:
            await cluster.kubectl(
                "wait", *(f"pod/{pod['metadata']['name']}" for pod in pods), "--for=delete", "--timeout=30s"
            )
        stopped = json.loads(await cluster.kubectl("get", "deployment/sandbox-controller", "-o", "json"))
        # This is the old guard, observed false before either control proceeds.
        assert stopped["status"].get("readyReplicas") != 1
        yield
    finally:
        await cluster.kubectl("scale", "deployment/sandbox-controller", "--replicas=1")
        await cluster.kubectl(
            "wait", "deployment/sandbox-controller", "--for=jsonpath={.status.readyReplicas}=1", "--timeout=30s"
        )


async def test_storage_refuses_a_controller_that_never_recovers(cluster: Cluster) -> None:
    async with _stopped_controller(cluster):
        with pytest.raises(RuntimeError, match="timed out"):
            await storage._ready_controller(cluster, case="stuck-controller-control")


@pytest.mark.timeout(300)
async def test_storage_keeps_all_probes_after_controller_recovery(
    cluster: Cluster, client: Client, dispatcher: SessionDispatcherClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    wait_started = asyncio.Event()
    original_kubectl = Cluster.kubectl

    async def observe_wait(self: Cluster, *args: str, **kwargs: Any) -> str:
        if args[:2] == ("wait", "deployment/sandbox-controller"):
            wait_started.set()
        return await original_kubectl(self, *args, **kwargs)

    async with _stopped_controller(cluster), asyncio.TaskGroup() as tasks:
        monkeypatch.setattr(Cluster, "kubectl", observe_wait)
        scenario = tasks.create_task(
            storage.fresh_workspace_storage_survives_replacement(
                cluster, client, dispatcher, Executor(template=POD_TEMPLATE, cwd="/state/work"), vm_storage=None
            )
        )
        async with asyncio.timeout(10):
            await wait_started.wait()
        assert not scenario.done()
        await cluster.kubectl("scale", "deployment/sandbox-controller", "--replicas=1")
        # TaskGroup awaits the entire original storage scenario, including its
        # refusal, suspend/resume, retained-volume, file-content and cleanup probes.


if __name__ == "__main__":
    pytest_bazel.main()
