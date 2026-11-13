"""Gateway reconnect-budget exhaustion with an in-flight central-sampler executor read."""

from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_retry_exhaustion_reports_unknown_and_accepts_next_input(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_retry_exhaustion_reports_unknown_and_accepts_next_input(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-budget")
    )


if __name__ == "__main__":
    pytest_bazel.main()
