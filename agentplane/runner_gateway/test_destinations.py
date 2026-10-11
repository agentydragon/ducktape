"""Runner destinations resolved against a fake API server's Sandbox and Pod."""

import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, SANDBOX, Cluster


async def test_successor_pod_same_sandbox_and_account_keeps_destination(cluster: Cluster) -> None:
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    await destinations.resolve(DESTINATION, writes=False)
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-successor-pod"
    # Raises DestinationUnavailableError for a Pod it would not route to.
    await destinations.resolve(DESTINATION, writes=False)


if __name__ == "__main__":
    pytest_bazel.main()
