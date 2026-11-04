"""Executable dispatcher scenarios sharing one k3s stack."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.scenarios import dispatcher


@pytest.fixture
def crictl_command(deployment: Stack) -> tuple[str, ...]:
    return (str(deployment.k3s.binary), "crictl")


@pytest.mark.timeout(600)
@pytest.mark.parametrize("replica_failover", [False, True], ids=["queue", "replicas"])
async def test_deployed_dispatcher_waits_for_ready_then_records_runner_admission(
    crictl_command: tuple[str, ...], cluster: Cluster, replica_failover: bool
) -> None:
    await dispatcher.deployed_dispatcher_waits_for_ready_then_records_runner_admission(
        crictl_command, cluster, replica_failover
    )


@pytest.mark.timeout(600)
async def test_deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends(
    crictl_command: tuple[str, ...], cluster: Cluster
) -> None:
    await dispatcher.deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends(
        crictl_command, cluster
    )


if __name__ == "__main__":
    pytest_bazel.main()
