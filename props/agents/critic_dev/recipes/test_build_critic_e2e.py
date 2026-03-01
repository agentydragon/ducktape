"""E2E test: mocked LLM agent invokes build_critic.sh from its container runfiles.

Tests that build_critic.sh works when called via exec tool from a critic-dev
agent container. The script is baked into the OCI image and located via
importlib.resources at runtime.
"""

from __future__ import annotations

import logging

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.agents.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
)
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import ClusterMemberSpec
from props.core.agent_types import TargetMetric
from props.core.eval_api_models import CriticRunStatus, GradingStatusResponse, RunCriticRequest, StartCriticResponse
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.db.models import AgentRun, AgentRunStatus, GradingEdge, ReportedIssue

logger = logging.getLogger(__name__)


@pytest.mark.timeout(300)
async def test_build_critic_sh_via_agent(
    synced_db, e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image
):
    """Verify build_critic.sh works when invoked by a mocked critic-dev agent.

    The script and custom_critic_for_test.py are both baked into the critic-dev
    OCI image. The mock LLM:
    1. Locates both files via importlib.resources
    2. Invokes build_critic.sh with the custom main.py
    3. Starts a critic with the resulting digest
    4. Verifies the custom image runs and produces a reported issue
    """
    snapshot_slug = SnapshotSlug(test_snapshot)

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        # Locate build_critic.sh via importlib.resources. The script resolves
        # REGISTRY from PROPS_BACKEND_URL and relative paths from SCRIPT_DIR,
        # so custom_critic_for_test.py (a sibling file) needs no absolute path.
        build_cmd = (
            'SCRIPT=$(python3 -c "import importlib.resources; '
            "print(importlib.resources.files('props') / 'agents/critic_dev/recipes/build_critic.sh')\") && "
            "bash $SCRIPT custom_critic_for_test.py test-variant"
        )
        result = yield from m.exec_roundtrip(["sh", "-c", build_cmd], timeout_ms=120000)
        assert_that(result, exited_successfully())
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.truncated_text
        new_digest = stdout.strip().split("\n")[-1]
        logger.info(f"build_critic.sh produced digest: {new_digest}")
        assert new_digest.startswith("sha256:"), f"Expected sha256 digest, got: {new_digest!r}"

        # Start the custom critic image
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        start_output: StartCriticResponse = yield from m.start_critic_roundtrip(
            RunCriticRequest(
                definition_id=DefinitionId(new_digest),
                example=example,
                timeout_seconds=120,
                budget_usd=1.0,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
            )
        )
        critic_run_id = start_output.critic_run_id
        logger.info(f"Custom critic run: {critic_run_id}")

        # Wait for critic to finish
        completed: CriticRunStatus = yield from m.wait_until_critic_completed_roundtrip(
            critic_run_id, timeout_seconds=120
        )
        logger.info(f"Critic completed: status={completed.status}")

        # Wait for grading
        wait_output: GradingStatusResponse = yield from m.wait_until_graded_roundtrip(critic_run_id, timeout_seconds=60)
        logger.info(f"Grading complete: total_credit={wait_output.total_credit}")

        yield m.report_success()

    @GraderMock.mock(check_consumed=False)
    def grader_mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        drift = yield from m.get_drift_roundtrip()
        run_id = drift.grading[0].critique_run_id

        yield from m.fill_remaining_roundtrip(run_id, "build-script-test-issue", 4, "Mock: no GT matches")

        drift = yield from m.get_drift_roundtrip()
        assert len(drift.clustering) == 1

        yield from m.create_cluster_roundtrip(
            "novel-issues",
            "Unmatched issues from build_critic.sh test",
            [ClusterMemberSpec(run=run_id, issue_id="build-script-test-issue", rationale="Novel issue")],
        )

        yield from m.sleep_forever("All edges graded and clustered")

    mocks = {ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock, ORCHESTRATION_GRADER_MODEL: grader_mock}
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        grader_image_resolved = stack.resolved_images["grader"]
        opt_image = stack.resolved_images["critic_dev_optimize"]

        grader_handle = await stack.registry.start_snapshot_grader(
            image=grader_image_resolved, snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL
        )

        async with grader_handle:
            run_id = await stack.registry.run_critic_dev_optimize(
                image=opt_image,
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.EXITED

                # Verify the build_critic.sh-produced image created the expected issue
                issues = session.query(ReportedIssue).filter_by(issue_id="build-script-test-issue").all()
                assert len(issues) == 1, f"Expected 1 issue from build_critic.sh, got {len(issues)}"

                # Verify grading edges were created
                critic_run_id = issues[0].agent_run_id
                edges = session.query(GradingEdge).filter_by(critique_run_id=critic_run_id).all()
                assert len(edges) > 0, "Expected grading edges for the build_critic.sh-produced critic run"


if __name__ == "__main__":
    pytest_bazel.main()
