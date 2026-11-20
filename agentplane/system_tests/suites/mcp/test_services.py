"""Central sampler Action admission and code-mode concurrency on the App-backed stack."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep //agentplane/system_tests/suites/mcp:operator_fixtures
pytest_plugins = (
    "agentplane.system_tests.suites.mcp.operator_fixtures",
    "agentplane.system_tests.deployment.pytest_plugin",
)


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_central_sampler_action_wait_keeps_other_code_mode_cells_live(
    cluster: Cluster, scripted: ScriptedUpstream, operator_bff: httpx.AsyncClient
) -> None:
    await central_sampler.delegated_action_wait_keeps_code_mode_live(
        cluster, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-actions"), operator_bff
    )


if __name__ == "__main__":
    pytest_bazel.main()
