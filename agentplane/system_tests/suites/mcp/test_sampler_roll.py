"""Sampler-only Pod replacement while a confirmed turn owns live executor work."""

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
async def test_sampler_roll_retains_active_turn_and_history_accepts_next_input(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.sampler_roll_retains_active_turn_and_history_accepts_next_input(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/sampler-roll")
    )


if __name__ == "__main__":
    pytest_bazel.main()
