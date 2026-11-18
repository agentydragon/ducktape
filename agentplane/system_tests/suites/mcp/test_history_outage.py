"""History downtime during a confirmed central-sampler turn and subsequent input."""

from pathlib import Path

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_history_outage_backfills_completed_turn(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.history_outage_backfills_completed_turn(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/history-outage")
    )
