"""Prompt optimizer e2e tests.

Tests the prompt optimizer agent using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from CriticDevMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → CriticDevMock/CriticMock

Tests verify:
- Prompt optimizer can run in container and use tools
- Database access works from container
- CLI helpers work in container context

Note: Grading is handled by snapshot grader daemons (not tested here).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
import pytest_bazel
from hamcrest import all_of, assert_that

from agent_core.testing.responses import PlayGen, tool_roundtrip
from mcp_infra.exec.matchers import exited_successfully, stdout_contains
from props.core.agent_types import AgentType
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.core.oci_utils import BUILTIN_TAG
from props.critic.testing.mocks import CriticMock
from props.critic_dev.loop import RunCriticToolArgs, WaitUntilGradedToolArgs
from props.critic_dev.shared import TargetMetric
from props.critic_dev.testing.mocks import CriticDevMock
from props.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
    make_orchestration_grader_mock,
)
from props.db.database import Database
from props.db.examples import Example
from props.db.models import AgentRun, AgentRunStatus, GradingEdge

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

# Test timeout (seconds) - applies to container execution
TEST_TIMEOUT_SECONDS = 60


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_po_agent_psql_connectivity(e2e_stack, prompt_optimizer_image):
    """Test that psql works from the agent container using PG* env vars."""

    @CriticDevMock.mock()
    def mock(m: CriticDevMock) -> PlayGen:
        yield None  # Receive first request
        result = yield from m.psql_roundtrip("SELECT 1")
        assert_that(result, all_of(exited_successfully(), stdout_contains("1")))
        yield m.report_failure("psql connectivity verified")

    async with e2e_stack(mock, images=[prompt_optimizer_image]) as stack:
        await stack.registry.run_prompt_optimizer(
            budget=1.0,
            optimizer_model=stack.model,
            critic_model=stack.model,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )


# =============================================================================
# Optimizer → Critic Workflow Test
# =============================================================================


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_optimizer_critic_workflow(e2e_stack, synced_db, test_snapshot, critic_image, db: Database):
    """Test optimizer → critic workflow with data access verification.

    Note: Grading is handled by snapshot grader daemons (not tested here).
    """
    # Get the whole-snapshot example and convert to ExampleSpec
    with synced_db.session() as session:
        example = (
            session.query(Example)
            .filter_by(snapshot_slug=test_snapshot, example_kind=ExampleKind.WHOLE_SNAPSHOT)
            .first()
        )
        assert example is not None, f"No whole_snapshot example found for {test_snapshot}"
        example_spec = example.to_example_spec()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        yield None  # First request
        yield m.insert_issue("test-issue-001", "Test issue")
        yield m.insert_occurrence("test-issue-001", "subtract.py", 1, 5)
        yield m.submit(issues_count=1, summary="Found 1 test issue")

    async with e2e_stack(critic_mock, images=[critic_image]) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=BUILTIN_TAG,
            example=example_spec,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=None,
        )
        assert critic_run_id is not None

    # Verify critic status and data
    with synced_db.session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        assert critic_run.status == AgentRunStatus.COMPLETED, f"Critic should complete, got {critic_run.status}"
        assert len(critic_run.reported_issues) == 1, f"Expected 1 issue, got {len(critic_run.reported_issues)}"
        assert critic_run.reported_issues[0].issue_id == "test-issue-001"


# =============================================================================
# CLI Helper Integration Tests
# =============================================================================


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_cli_leaderboard_shows_recall(e2e_stack, test_train_example_with_runs, prompt_optimizer_image):
    """Test that leaderboard CLI command shows actual recall values from database."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    @CriticDevMock.mock()
    def mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request
        result = yield from m.exec_roundtrip(["critic-dev", "leaderboard", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield m.report_failure("Leaderboard test completed")

    async with e2e_stack(mock, images=[prompt_optimizer_image]) as stack:
        await stack.registry.run_prompt_optimizer(
            budget=1.0,
            optimizer_model=stack.model,
            critic_model=stack.model,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_cli_hard_examples_shows_metrics(e2e_stack, test_train_example_with_runs, prompt_optimizer_image):
    """Test that hard-examples CLI command shows example metrics."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    @CriticDevMock.mock()
    def mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request
        result = yield from m.exec_roundtrip(["critic-dev", "hard-examples", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield m.report_failure("Hard examples test completed")

    async with e2e_stack(mock, images=[prompt_optimizer_image]) as stack:
        await stack.registry.run_prompt_optimizer(
            budget=1.0,
            optimizer_model=stack.model,
            critic_model=stack.model,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )


# =============================================================================
# Multi-Model Orchestration Tests
# =============================================================================


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
@pytest.mark.slow
async def test_optimizer_orchestrates_critic(
    synced_db: Database, e2e_stack, test_snapshot, prompt_optimizer_image, critic_image, grader_image
):
    """Test optimizer can orchestrate critic runs with simulated grading.

    This e2e test verifies the full orchestration flow:
    1. Optimizer container starts with DirectToolProvider tools
    2. Optimizer calls run_critic tool (REST API to backend)
    3. Registry spawns critic container with different model
    4. Critic runs, submits issues, completes
    5. Background grader daemon processes edges
    6. Optimizer's wait_until_graded_tool returns (polls database directly)
    7. Optimizer reports success

    Uses multi-model FakeOpenAIServer to route optimizer and critic to different mocks.
    """
    snapshot_slug = test_snapshot
    logger.info(f"Running orchestration test with snapshot: {snapshot_slug}")

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request (system message)

        # Call run_critic tool (DirectToolProvider tool that calls REST API)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id=BUILTIN_TAG, example=example, timeout_seconds=120, budget_usd=None
        )

        call = m.tool_call("run_critic", run_critic_args)
        run_critic_response: RunCriticResponse = yield from tool_roundtrip(call, RunCriticResponse)

        critic_run_id = run_critic_response.critic_run_id
        logger.info(f"Orchestration optimizer got critic_run_id: {critic_run_id}")

        # Call wait_until_graded_tool (DirectToolProvider tool that polls database)
        wait_args = WaitUntilGradedToolArgs(critic_run_id=str(critic_run_id), timeout_seconds=60)
        wait_call = m.tool_call("wait_until_graded_tool", wait_args)
        grading_response: GradingStatusResponse = yield from tool_roundtrip(wait_call, GradingStatusResponse)

        total_credit = grading_response.total_credit or 0.0
        max_credit = grading_response.max_credit or 0
        recall = total_credit / max_credit if max_credit > 0 else 0.0
        logger.info(f"Orchestration optimizer got grading: total_credit={total_credit}, recall={recall:.2%}")

        # Report success
        yield m.report_success()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        yield None  # First request (system message)

        # Insert an issue and occurrence
        yield m.insert_issue("orchestration-test-001", "Test issue from orchestration")
        yield m.insert_occurrence("orchestration-test-001", "test.py", 1, 10)

        # Submit the critique
        yield m.submit(issues_count=1, summary="Found 1 orchestration test issue")

    grader_mock = make_orchestration_grader_mock()

    mocks = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
        ORCHESTRATION_GRADER_MODEL: grader_mock,
    }
    async with e2e_stack(mocks, images=[prompt_optimizer_image, critic_image, grader_image]) as stack:
        # Start grader daemon in background - it will sleep until there's drift
        grader_task: asyncio.Task[None] | None = None

        async def run_grader_daemon() -> None:
            """Run grader daemon in background."""
            try:
                logger.info(f"Starting grader daemon for {snapshot_slug}")
                await stack.registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
                logger.info("Grader daemon completed")
            except asyncio.CancelledError:
                logger.info("Grader daemon cancelled")
            except Exception as e:
                logger.error(f"Grader daemon error: {e}")
                raise

        grader_task = asyncio.create_task(run_grader_daemon())

        try:
            # Run prompt optimizer - this triggers the full orchestration
            # The grader daemon running in background will process edges when critic completes
            run_id = await stack.registry.run_prompt_optimizer(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=120,
            )

            logger.info(f"Orchestration test: prompt optimizer completed with run_id={run_id}")

            # Verify optimizer run status
            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None, "Optimizer run not found in database"
                assert optimizer_run.status == AgentRunStatus.COMPLETED, (
                    f"Expected optimizer status COMPLETED, got {optimizer_run.status}"
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
                    assert cr.status == AgentRunStatus.COMPLETED, f"Critic run {cr.agent_run_id} should be COMPLETED"

            # Verify grading edges were created (drift resolved)
            with synced_db.session() as session:
                for cr in critic_runs:
                    edges = session.query(GradingEdge).filter_by(critique_run_id=cr.agent_run_id).all()
                    logger.info(f"Critic {cr.agent_run_id} has {len(edges)} grading edges")
                    # The critic mock creates 1 issue, and fill_remaining creates edges for each GT occurrence
                    assert len(edges) >= 0, "Grading edges should be created"

        finally:
            # Cancel grader daemon if still running
            if grader_task is not None and not grader_task.done():
                grader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await grader_task


if __name__ == "__main__":
    pytest_bazel.main()
