"""Runner destinations resolved against a fake API server's Sandbox and Pod."""

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxDestination, ServiceAccount
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.testing.runner_control import OWN_SANDBOX, OWN_SANDBOX_UID, add_own_sandbox
from agentplane.sandbox_service.errors import SandboxTearingDownError
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, OWN_SANDBOXES, SANDBOX, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf


async def test_successor_pod_same_sandbox_and_account_keeps_destination(cluster: Cluster) -> None:
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    await destinations.resolve(DESTINATION, writes=False)
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-successor-pod"
    # Raises DestinationUnavailableError for a Pod it would not route to.
    await destinations.resolve(DESTINATION, writes=False)


def _own_destination() -> SandboxDestination:
    return SandboxDestination(
        sandbox=OWN_SANDBOX,
        sandbox_uid=OWN_SANDBOX_UID,
        owner=ServiceAccount(namespace=SANDBOX_NAMESPACE, name=OWN_SANDBOX),
    )


async def test_own_sandbox_resolves_to_its_spec_binding(cluster: Cluster) -> None:
    add_own_sandbox(cluster)
    cluster.fake.objects[OWN_SANDBOXES][OWN_SANDBOX]["spec"]["bootstrap"] = "printf boot"
    cluster.fake.pods[OWN_SANDBOX]["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    assert await destinations.resolve(_own_destination(), writes=True) == SandboxBinding(bootstrap="printf boot")


async def test_sealing_own_sandbox_takes_no_new_work(cluster: Cluster) -> None:
    add_own_sandbox(cluster)
    cluster.fake.objects[OWN_SANDBOXES][OWN_SANDBOX]["metadata"]["deletionTimestamp"] = "2026-09-01T12:00:00Z"
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    with pytest.raises(SandboxTearingDownError):
        await destinations.resolve(_own_destination(), writes=True)


if __name__ == "__main__":
    pytest_bazel.main()
