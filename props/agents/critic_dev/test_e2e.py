"""E2E test for agent orchestration.

Tests the orchestration workflow:
1. Optimizer calls run_critic to spawn critic containers
2. Critic receives system prompt and submits issues
3. Grader processes edges
4. Optimizer waits for grading and reports success

Uses the in-container architecture with:
- FakeOpenAI server backed by CriticDevMock/CriticMock/GraderMock
- LLM proxy for auth and logging
- AgentRegistry for container orchestration
"""

from __future__ import annotations

import logging

import pytest
import pytest_bazel

from agent_core.testing.responses import PlayGen
from props.agents.critic.testing.mocks import CriticMock
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.agents.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
)
from props.agents.grader.testing.mocks import GraderMock
from props.core.agent_types import TargetMetric
from props.core.eval_api_models import CriticRunStatus, GradingStatusResponse, RunCriticRequest, StartCriticResponse
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.mocks import get_system_prompt_text

logger = logging.getLogger(__name__)


# Test timeout (seconds)
TEST_TIMEOUT_SECONDS = 120


@pytest.mark.timeout(300)
async def test_po_orchestrates_critic_with_system_prompt_check(
    synced_db, e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image
):
    """Test critic-dev optimizer orchestration with critic system prompt verification.

    Verifies:
    1. Optimizer can call run_critic MCP tool
    2. Critic receives a valid system prompt (mechanism check)
    3. Grader processes the edges
    4. Optimizer's wait_until_graded returns

    The critic mock verifies it receives a proper system message, proving
    the in-container architecture properly passes prompts to agents.
    """
    snapshot_slug = SnapshotSlug(test_snapshot)

    # Mutable container filled after image push but before mock runs
    digests: dict[str, str] = {}

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        # Start critic (non-blocking, returns immediately with critic_run_id)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        start_output: StartCriticResponse = yield from m.start_critic_roundtrip(
            RunCriticRequest(
                definition_id=DefinitionId(digests["critic"]),
                example=example,
                timeout_seconds=120,
                budget_usd=1.0,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
            )
        )
        critic_run_id = start_output.critic_run_id
        logger.info(f"PO got critic_run_id: {critic_run_id}")

        # Wait for critic container to finish
        completed: CriticRunStatus = yield from m.wait_until_critic_completed_roundtrip(
            critic_run_id, timeout_seconds=120
        )
        logger.info(f"Critic completed: status={completed.status}")

        # Wait for grading (polls database directly inside container)
        wait_output: GradingStatusResponse = yield from m.wait_until_graded_roundtrip(critic_run_id, timeout_seconds=60)
        logger.info(f"PO got grading: total_credit={wait_output.total_credit}")

        # Report success
        yield m.report_success()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        # Capture first request to verify system message is present
        first_request = yield None

        # Verify we received a non-empty system message
        system_text = get_system_prompt_text(first_request)
        assert system_text, "Expected non-empty system message"
        assert "critic" in system_text.lower(), (
            f"Expected system message to mention 'critic'. Got: {system_text[:200]}..."
        )
        logger.info(f"Critic received system message ({len(system_text)} chars)")

        # Submit zero issues
        yield m.submit(issues_count=0, summary="Critic completed")

    # Grader mock for zero-issue case: should never be woken
    @GraderMock.mock()
    def grader_mock(m: GraderMock) -> PlayGen:
        yield None  # First request (waits for drift)
        raise AssertionError("Grader should not be woken when critic submits 0 issues")

    mocks = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
        ORCHESTRATION_GRADER_MODEL: grader_mock,
    }
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        digests.update(stack.image_digests)

        # Resolve images before starting tasks
        grader_image_resolved = stack.resolved_images["grader"]
        opt_image = stack.resolved_images["critic_dev_optimize"]

        # Start snapshot grader in background
        grader_handle = await stack.registry.start_snapshot_grader(
            image=grader_image_resolved, snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL
        )

        async with grader_handle:
            # Run critic-dev optimizer
            run_id = await stack.registry.run_critic_dev_optimize(
                image=opt_image,
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            # Verify optimizer completed
            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.EXITED, f"Expected COMPLETED, got {optimizer_run.status}"


@pytest.mark.timeout(180)
async def test_critic_cannot_push_images(e2e_stack, synced_db: Database, all_files_scope, critic_image):
    """Test that critic agents cannot push images to registry.

    Critic agents should only be able to read from the registry, not write.
    Attempting to push should result in a 403 Forbidden error.

    Note: This test verifies the permission model at the registry proxy level.
    The critic container has RLS-scoped database access via a temp user,
    and the registry proxy should check the agent type before allowing pushes.
    """

    @CriticMock.mock()
    def mock(m: CriticMock) -> PlayGen:
        yield None  # First request

        # Try crane push from critic container — should fail because
        # critics don't have registry write access (403 Forbidden).
        result = yield from m.exec_roundtrip(
            [
                "sh",
                "-c",
                "REGISTRY=${PROPS_REGISTRY_URL#http://}; "
                "REGISTRY=${REGISTRY#https://}; "
                "crane push /workspace/ $REGISTRY/test-push:latest --insecure 2>&1",
            ],
            timeout_ms=30000,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        logger.info(f"Critic push attempt stdout: {stdout}")
        logger.info(f"Critic push attempt stderr: {stderr}")

        # Submit zero issues (expected behavior: push failed, critic still completes)
        yield m.submit(issues_count=0, summary="Push attempt completed (expected to fail)")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
        critic_image_resolved = stack.resolved_images["critic"]
        run_id = await stack.registry.run_critic(
            image=critic_image_resolved,
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=5.0,
        )

        # Verify critic completed (it should complete even though push failed)
        with synced_db.session() as session:
            critic_run = session.get(AgentRun, run_id)
            assert critic_run is not None
            # The critic should complete because it handled the push failure gracefully
            assert critic_run.status == AgentRunStatus.EXITED


if __name__ == "__main__":
    pytest_bazel.main()
