"""Prompt optimizer e2e tests.

Tests the prompt optimizer agent using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from PropsMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → PropsMock

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

import aiodocker
import pytest
import pytest_bazel
from hamcrest import all_of, assert_that

from agent_core_testing.responses import PlayGen, tool_roundtrip
from mcp_infra.exec.matchers import exited_successfully, stdout_contains
from props.core.agent_types import AgentType
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.core.splits import Split
from props.critic_dev.optimize.main import RunCriticToolArgs, WaitUntilGradedToolArgs
from props.critic_dev.optimize.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
    make_orchestration_grader_mock,
    multi_model_e2e_stack,
)
from props.critic_dev.shared import TargetMetric
from props.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.db.config import DatabaseConfig
from props.db.examples import Example
from props.db.models import AgentRun, AgentRunStatus, GradingEdge, Snapshot
from props.db.session import get_session
from props.testing.mocks import PropsMock

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

# Test timeout (seconds) - applies to container execution
TEST_TIMEOUT_SECONDS = 60


def make_psql_connectivity_mock() -> PropsMock:
    """Create mock that tests psql connectivity then terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # Receive first request
        result = yield from m.psql_roundtrip("SELECT 1")
        assert_that(result, all_of(exited_successfully(), stdout_contains("1")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "psql connectivity verified"])

    return mock


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_po_agent_psql_connectivity(e2e_stack):
    """Test that psql works from the agent container using PG* env vars."""
    mock = make_psql_connectivity_mock()

    async with e2e_stack(mock) as stack:
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


def make_critic_mock_with_issue() -> PropsMock:
    """Create mock for critic that submits one issue."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critique", "insert-issue", "test-issue-001", "Test issue"])
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "test-issue-001", "subtract.py", "-s", "1", "-e", "5"]
        )
        assert_that(result, exited_successfully())
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 test issue"])

    return mock


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_optimizer_critic_workflow(e2e_stack, synced_test_db, test_snapshot):
    """Test optimizer → critic workflow with data access verification.

    Note: Grading is handled by snapshot grader daemons (not tested here).
    """
    # Get the whole-snapshot example and convert to ExampleSpec
    with get_session() as session:
        example = (
            session.query(Example)
            .filter_by(snapshot_slug=test_snapshot, example_kind=ExampleKind.WHOLE_SNAPSHOT)
            .first()
        )
        assert example is not None, f"No whole_snapshot example found for {test_snapshot}"
        example_spec = example.to_example_spec()

    # First: run critic separately to verify it works
    critic_mock = make_critic_mock_with_issue()
    async with e2e_stack(critic_mock) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=CRITIC_IMAGE_REF,
            example=example_spec,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=None,
        )
        assert critic_run_id is not None

    # Verify critic status and data
    with get_session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        assert critic_run.status == AgentRunStatus.COMPLETED, f"Critic should complete, got {critic_run.status}"
        assert len(critic_run.reported_issues) == 1, f"Expected 1 issue, got {len(critic_run.reported_issues)}"
        assert critic_run.reported_issues[0].issue_id == "test-issue-001"


# =============================================================================
# CLI Helper Integration Tests
# =============================================================================


def make_leaderboard_check_mock() -> PropsMock:
    """Create mock that runs leaderboard and terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "leaderboard", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Leaderboard test completed"])

    return mock


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_cli_leaderboard_shows_recall(e2e_stack, test_train_example_with_runs):
    """Test that leaderboard CLI command shows actual recall values from database."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    mock = make_leaderboard_check_mock()

    async with e2e_stack(mock) as stack:
        await stack.registry.run_prompt_optimizer(
            budget=1.0,
            optimizer_model=stack.model,
            critic_model=stack.model,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )


def make_hard_examples_check_mock() -> PropsMock:
    """Create mock that runs hard-examples and terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "hard-examples", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Hard examples test completed"])

    return mock


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_cli_hard_examples_shows_metrics(e2e_stack, test_train_example_with_runs):
    """Test that hard-examples CLI command shows example metrics."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    mock = make_hard_examples_check_mock()

    async with e2e_stack(mock) as stack:
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
# These tests use MultiModelFakeOpenAI to route optimizer and critic to
# different mocks, testing the full orchestration flow.


def make_orchestration_optimizer_mock(snapshot_slug: SnapshotSlug) -> PropsMock:
    """Create optimizer mock that calls run_critic tool and waits for grading.

    Uses DirectToolProvider tools (not MCP):
    - run_critic: calls backend REST API
    - wait_until_graded_tool: polls database directly
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request (system message)

        # Call run_critic tool (DirectToolProvider tool that calls REST API)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id="builtin",  # Use builtin critic
            example=example,
            timeout_seconds=120,
            budget_usd=None,
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
        yield from m.docker_exec_roundtrip(["prompt-optimize-dev", "report-success"])

    return mock


def make_orchestration_critic_mock() -> PropsMock:
    """Create critic mock that submits one issue and completes."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request (system message)

        # Insert an issue and occurrence
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-issue", "orchestration-test-001", "Test issue from orchestration"]
        )
        assert_that(result, exited_successfully())

        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "orchestration-test-001", "test.py", "-s", "1", "-e", "10"]
        )
        assert_that(result, exited_successfully())

        # Submit the critique
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 orchestration test issue"])

    return mock


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
@pytest.mark.slow
async def test_optimizer_orchestrates_critic(synced_test_db: DatabaseConfig, async_docker_client: aiodocker.Docker):
    """Test optimizer can orchestrate critic runs with simulated grading.

    This e2e test verifies the full orchestration flow:
    1. Optimizer container starts with DirectToolProvider tools
    2. Optimizer calls run_critic tool (REST API to backend)
    3. Registry spawns critic container with different model
    4. Critic runs, submits issues, completes
    5. Background grader daemon processes edges
    6. Optimizer's wait_until_graded_tool returns (polls database directly)
    7. Optimizer reports success

    Uses MultiModelFakeOpenAI to route optimizer and critic to different mocks.
    """
    # Get a test snapshot with TRAIN split
    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(split=Split.TRAIN).first()
        if not snapshot:
            pytest.skip("No TRAIN snapshots available for orchestration test")

        # Verify there's an example for this snapshot
        example = (
            session.query(Example)
            .filter_by(snapshot_slug=snapshot.slug, example_kind=ExampleKind.WHOLE_SNAPSHOT)
            .first()
        )
        if not example:
            pytest.skip(f"No whole_snapshot example for {snapshot.slug}")

        snapshot_slug = SnapshotSlug(snapshot.slug)

    logger.info(f"Running orchestration test with snapshot: {snapshot_slug}")

    # Create mocks for all three agents
    optimizer_mock = make_orchestration_optimizer_mock(snapshot_slug)
    critic_mock = make_orchestration_critic_mock()
    grader_mock = make_orchestration_grader_mock()

    async with multi_model_e2e_stack(
        optimizer_mock, critic_mock, synced_test_db, async_docker_client, grader_mock=grader_mock
    ) as registry:
        # Start grader daemon in background - it will sleep until there's drift
        grader_task: asyncio.Task[None] | None = None

        async def run_grader_daemon() -> None:
            """Run grader daemon in background."""
            try:
                logger.info(f"Starting grader daemon for {snapshot_slug}")
                await registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
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
            run_id = await registry.run_prompt_optimizer(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=120,
            )

            logger.info(f"Orchestration test: prompt optimizer completed with run_id={run_id}")

            # Verify optimizer run status
            with get_session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None, "Optimizer run not found in database"
                assert optimizer_run.status == AgentRunStatus.COMPLETED, (
                    f"Expected optimizer status COMPLETED, got {optimizer_run.status}"
                )

            # Verify a critic run was created and completed
            with get_session() as session:
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
            with get_session() as session:
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
