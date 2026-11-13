"""Controller-managed deletion with an already-routed Session command."""

import pytest
import pytest_bazel

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios.session_deletion import (
    deployed_session_command_cannot_cross_deletion,
    managed_cli_reads_retained_history_with_identity_uid_scope,
)


@pytest.mark.timeout(600)
@pytest.mark.scripted_model
async def test_session_command_cannot_cross_deletion(
    deployment: Stack, cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    await deployed_session_command_cannot_cross_deletion((str(deployment.k3s.binary), "crictl"), cluster, scripted)


@pytest.mark.timeout(600)
@pytest.mark.scripted_model
async def test_managed_cli_history_identity_uid_scope(
    deployment: Stack, cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    del deployment
    await managed_cli_reads_retained_history_with_identity_uid_scope(cluster, scripted)


if __name__ == "__main__":
    pytest_bazel.main()
