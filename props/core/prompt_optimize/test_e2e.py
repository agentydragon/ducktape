"""Prompt optimizer e2e tests.

Tests the prompt optimizer agent using:
- Real Docker containers
- Real PostgreSQL database with temporary RLS-scoped users
- Mocked OpenAI responses
- HTTP MCP transport with bearer token auth

Tests verify:
- Prompt optimizer orchestrates critic runs
- Critic can read and write to database
- All agents submit via MCP

Note: Grading is handled by snapshot grader daemons (not tested here).
"""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import all_of, assert_that

from agent_core_testing.responses import PlayGen
from agent_core_testing.steps import exited_successfully, stdout_contains
from props.core.db.config import DatabaseConfig
from props.core.db.examples import Example
from props.core.db.models import AgentRun, AgentRunStatus
from props.core.db.session import get_session
from props.core.models.examples import ExampleKind
from props.core.prompt_optimize.prompt_optimizer import run_prompt_optimizer
from props.core.prompt_optimize.target_metric import TargetMetric
from props.testing.mocks import PropsMock

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.mark.timeout(30)
@pytest.mark.requires_docker
async def test_po_agent_psql_connectivity(synced_test_db: DatabaseConfig, noop_openai_client, async_docker_client):
    """Test that psql works from the agent container using PG* env vars."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # Receive first request
        result = yield from m.psql_roundtrip("SELECT 1")
        assert_that(result, all_of(exited_successfully(), stdout_contains("1")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "psql connectivity verified"])

    await run_prompt_optimizer(
        budget=1.0,
        optimizer_client=mock,
        critic_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )


# =============================================================================
# Optimizer → Critic Workflow Test
# =============================================================================


def make_critic_mock_with_issue() -> PropsMock:
    """Create mock for critic that submits one issue."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-issue", "test-issue-001", "Test issue"]
        )
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "test-issue-001", "subtract.py", "-s", "1", "-e", "5"]
        )
        assert_that(result, exited_successfully())
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 test issue"])

    return mock


@pytest.mark.timeout(120)
@pytest.mark.requires_docker
async def test_optimizer_critic_workflow(
    synced_test_db: DatabaseConfig, async_docker_client, test_snapshot, noop_openai_client, test_registry
):
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

    # Optimizer mock: verify DB access then terminate
    @PropsMock.mock()
    def optimizer_mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.psql_roundtrip("SELECT 1")
        assert_that(result, all_of(exited_successfully(), stdout_contains("1")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Setup verified, proceeding"])

    # Run prompt optimizer (just to verify setup)
    await run_prompt_optimizer(
        budget=1.0,
        optimizer_client=optimizer_mock,
        critic_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )

    # Run critic using generator mock
    critic_mock = make_critic_mock_with_issue()
    critic_run_id = await test_registry.run_critic(
        image_ref="critic", example=example_spec, client=critic_mock, max_turns=100
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


@pytest.mark.timeout(60)
@pytest.mark.requires_docker
async def test_cli_leaderboard_shows_recall(
    synced_test_db: DatabaseConfig, noop_openai_client, async_docker_client, test_train_example_with_runs
):
    """Test that leaderboard CLI command shows actual recall values from database."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "leaderboard", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Leaderboard test completed"])

    await run_prompt_optimizer(
        budget=1.0,
        optimizer_client=mock,
        critic_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )


@pytest.mark.timeout(60)
@pytest.mark.requires_docker
async def test_cli_hard_examples_shows_metrics(
    synced_test_db: DatabaseConfig, noop_openai_client, async_docker_client, test_train_example_with_runs
):
    """Test that hard-examples CLI command shows example metrics."""
    example, _critic_run, _grader_run = test_train_example_with_runs
    assert example.recall_denominator == 4, "test-trivial should have 4 expected occurrences"

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "hard-examples", "--limit", "5"])
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Hard examples test completed"])

    await run_prompt_optimizer(
        budget=1.0,
        optimizer_client=mock,
        critic_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )


if __name__ == "__main__":
    pytest_bazel.main()
