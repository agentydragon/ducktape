"""Manual candidate: validate issuer rejection before mutating one fresh Firecracker guest."""

from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import workload_trust
from agentplane.system_tests.scenarios.firecracker import preserves_live_guest


@pytest.mark.scripted_model
async def test_gateway_issuer_and_live_state_share_one_fresh_firecracker_guest(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-firecracker-{uuid4().hex[:8]}", template="environment-firecracker")
    # Validate the fresh listener before creating a Session, rotating credentials or suspending.
    await workload_trust.a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(
        cluster, view, route=workload_trust.RunnerControlRoute.POD_IP
    )
    await preserves_live_guest(cluster, client, dispatcher, view, scripted)


if __name__ == "__main__":
    pytest_bazel.main()
