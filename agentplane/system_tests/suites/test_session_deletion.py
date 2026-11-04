"""Controller-managed deletion with an already-routed Session command."""

import pytest
import pytest_bazel

from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.k3s.stack import Stack
from agentplane.system_tests.scenarios.session_deletion import deployed_session_command_cannot_cross_deletion


@pytest.mark.timeout(600)
async def test_session_command_cannot_cross_deletion(deployment: Stack, cluster: Cluster) -> None:
    await deployed_session_command_cannot_cross_deletion((str(deployment.k3s.binary), "crictl"), cluster)


if __name__ == "__main__":
    pytest_bazel.main()
