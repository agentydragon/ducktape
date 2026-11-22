"""Exercise optimizer orchestration when the critic submits an issue for grading and clustering."""

from __future__ import annotations

import logging

import pytest_bazel

from openai_utils.testing.responses import PlayGen
from props.agents.critic.testing.mocks import CriticMock
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import ClusterMemberSpec
from props.core.agent_types import AgentType
from props.db.models import AgentRun, AgentRunStatus

# gazelle:include_dep //props/agents/testing:e2e_pytest_plugins
# gazelle:include_dep //props/agents/critic_dev/testing:orchestration_fixtures
pytest_plugins = ("props.agents.testing.e2e_pytest_plugins", "props.agents.critic_dev.testing.orchestration_fixtures")

logger = logging.getLogger(__name__)


async def test_optimizer_orchestrates_critic(synced_db, run_optimizer_critic):
    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        yield None  # First request (system message)

        # Insert an issue and occurrence
        yield m.insert_issue("orchestration-test-001", "Test issue from orchestration")
        yield m.insert_occurrence("orchestration-test-001", "test.py", 1, 10)

        # Submit the critique
        yield m.submit(issues_count=1, summary="Found 1 orchestration test issue")

    # Grader mock for "orchestration-test-001" on test.py
    # test.py matches: tp-003/occ-1, tp-004/occ-1, tp-005/occ-1, fp-001/fp-occ-1 = 4 edges
    @GraderMock.mock()
    def grader_mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # Get pending edges for orchestration-test-001
        drift = yield from m.get_drift_roundtrip()
        run_id = drift.grading[0].critique_run_id

        # Fill all 4 edges with credit=0
        yield from m.fill_remaining_roundtrip(run_id, "orchestration-test-001", 4, "Mock: no GT matches")

        # Issue has credit=0, appears in clustering
        drift = yield from m.get_drift_roundtrip()
        assert len(drift.clustering) == 1

        # Cluster the issue
        yield from m.create_cluster_roundtrip(
            "novel-issues",
            "Unmatched issues from orchestration",
            [ClusterMemberSpec(run=run_id, issue_id="orchestration-test-001", rationale="Novel issue")],
        )

        yield from m.sleep_forever("All edges graded and clustered")

    async with run_optimizer_critic(critic_mock, grader_mock) as run_id:
        logger.info(f"Orchestration test: critic-dev optimizer completed with run_id={run_id}")

        # Verify optimizer run status
        with synced_db.session() as session:
            optimizer_run = session.get(AgentRun, run_id)
            assert optimizer_run is not None, "Optimizer run not found in database"
            assert optimizer_run.status == AgentRunStatus.EXITED, (
                f"Expected optimizer status EXITED, got {optimizer_run.status}"
            )

        # Verify a critic run was created and completed
        with synced_db.session() as session:
            critic_runs = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
                    AgentRun.parent_agent_run_id == run_id,
                )
                .all()
            )
            assert len(critic_runs) >= 1, "Expected at least one critic run spawned by optimizer"
            for cr in critic_runs:
                assert cr.status == AgentRunStatus.EXITED, f"Critic run {cr.agent_run_id} should be EXITED"


if __name__ == "__main__":
    pytest_bazel.main()
