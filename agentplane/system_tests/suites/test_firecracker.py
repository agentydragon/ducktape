"""Hardware-backed Firecracker acceptance on the dedicated BuildBuddy KVM worker."""

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios.firecracker import preserves_live_guest


@pytest.mark.scripted_model
async def test_firecracker_preserves_live_guest(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    await preserves_live_guest(cluster, client, dispatcher, sandbox, scripted)


if __name__ == "__main__":
    pytest_bazel.main()
