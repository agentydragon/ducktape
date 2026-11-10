"""Abrupt Gateway loss with an in-flight central-sampler executor read."""

from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_crash_settles_live_central_read_and_accepts_next_input(
    cluster: Cluster, deployment: Stack, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_crash_settles_live_central_read_and_accepts_next_input(
        cluster,
        tmp_path,
        scripted,
        Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-crash"),
        (str(deployment.k3s.binary), "crictl"),
    )


if __name__ == "__main__":
    pytest_bazel.main()
