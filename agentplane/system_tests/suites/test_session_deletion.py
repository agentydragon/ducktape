"""Controller-managed deletion with an already-routed Session command."""

import pytest
import pytest_bazel

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios.session_deletion import deployed_session_command_cannot_cross_deletion


@pytest.mark.timeout(600)
@pytest.mark.scripted_model
async def test_session_command_cannot_cross_deletion(
    deployment: Stack, cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    await deployed_session_command_cannot_cross_deletion((str(deployment.k3s.binary), "crictl"), cluster, scripted)


if __name__ == "__main__":
    pytest_bazel.main()
