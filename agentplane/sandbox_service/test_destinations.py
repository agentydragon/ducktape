"""Runner destinations resolved against a fake API server's Sandbox and Pod."""

import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, SANDBOX, Cluster

RUNNER_PORT = 7000


async def test_successor_pod_same_sandbox_and_account_keeps_destination(cluster: Cluster) -> None:
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), RUNNER_PORT)
    first = await destinations.resolve(DESTINATION, writes=False)
    assert first.target == f"127.0.0.1:{RUNNER_PORT}"
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-successor-pod"
    cluster.fake.pods[SANDBOX]["status"]["podIP"] = "::1"
    resolved = await destinations.resolve(DESTINATION, writes=False)
    assert resolved.target == f"[::1]:{RUNNER_PORT}"


if __name__ == "__main__":
    pytest_bazel.main()
