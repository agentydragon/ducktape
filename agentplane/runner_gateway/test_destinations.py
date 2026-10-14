"""Runner destinations resolved against a fake API server's Sandbox and Pod."""

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.proto.sandboxes_pb2 import SandboxBinding
from agentplane.runner_gateway.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.runner_gateway.testing.runner_control import run_in_a_vm
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.errors import SandboxTearingDownError
from agentplane.sandboxes.testing.kubernetes import DESTINATION, SANDBOX, SandboxCluster

# gazelle:include_dep @pypi//protobuf


async def test_successor_pod_same_sandbox_and_account_keeps_destination(cluster: SandboxCluster) -> None:
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    await destinations.resolve(DESTINATION, writes=False)
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-successor-pod"
    # The Sandbox Controller reports the successor as the incarnation.
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["status"]["incarnation"]["uid"] = "test-successor-pod"
    # Raises DestinationUnavailableError for a Pod it would not route to.
    await destinations.resolve(DESTINATION, writes=False)


@pytest.mark.parametrize("vm", [False, True], ids=["pod", "vm"])
async def test_sandbox_resolves_to_its_spec_binding(cluster: SandboxCluster, vm: bool) -> None:
    if vm:
        run_in_a_vm(cluster)
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["bootstrap"] = "printf boot"
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    assert await destinations.resolve(DESTINATION, writes=True) == SandboxBinding(bootstrap="printf boot")


async def test_a_pod_the_controller_has_not_reported_is_not_a_destination(cluster: SandboxCluster) -> None:
    # A new incarnation under the same name, before the Sandbox Controller reports it.
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-unreported-pod-uid"
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    with pytest.raises(DestinationUnavailableError):
        await destinations.resolve(DESTINATION, writes=True)


async def test_sealing_sandbox_takes_no_new_work(cluster: SandboxCluster) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"]["deletionTimestamp"] = "2026-09-01T12:00:00Z"
    destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api))
    with pytest.raises(SandboxTearingDownError):
        await destinations.resolve(DESTINATION, writes=True)


if __name__ == "__main__":
    pytest_bazel.main()
