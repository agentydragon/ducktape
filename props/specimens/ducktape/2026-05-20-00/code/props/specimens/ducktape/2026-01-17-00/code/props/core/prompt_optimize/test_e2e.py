"""Prompt optimizer e2e tests.

Tests the prompt optimizer agent using:
- Real Docker containers
- Real PostgreSQL database with temporary RLS-scoped users
- Mocked OpenAI responses
- HTTP MCP transport with bearer token auth

Comprehensive 3-agent test verifies:
- Prompt optimizer orchestrates critic and grader runs
- Grader can read critic runs (reported_issues from the critique)
- Grader can read ground truth (true_positives, false_positives)
- Grader can write grading decisions
- All agents submit via MCP
"""

from __future__ import annotations

from uuid import UUID

import pytest
from hamcrest import all_of, assert_that

from agent_core_testing.openai_mock import CapturingOpenAIModel
from agent_core_testing.responses import PlayGen
from agent_core_testing.steps import exited_successfully, stdout_contains
from props.core.db.config import DatabaseConfig
from props.core.db.examples import Example
from props.core.db.models import AgentRun, AgentRunStatus, GradingEdge
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
        grader_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )


# =============================================================================
# Comprehensive 3-Agent Test: Optimizer → Critic → Grader
# =============================================================================


def make_critic_mock_with_issue() -> PropsMock:
    """Create mock for critic that submits one issue."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-issue", "test-issue-001", "Test issue for grader"]
        )
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "test-issue-001", "subtract.py", "-s", "1", "-e", "5"]
        )
        assert_that(result, exited_successfully())
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 test issue"])

    return mock


def make_grader_mock_with_data_access(critic_run_id: UUID) -> PropsMock:
    """Create mock for grader demonstrating full data access."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.psql_roundtrip(
            f"SELECT issue_id, rationale FROM reported_issues WHERE agent_run_id = '{critic_run_id}'"
        )
        assert_that(result, all_of(exited_successfully(), stdout_contains("test-issue-001")))
        result = yield from m.psql_roundtrip("SELECT tp_id, rationale FROM true_positives LIMIT 5")
        assert_that(result, exited_successfully())
        result = yield from m.psql_roundtrip("SELECT fp_id, rationale FROM false_positives LIMIT 5")
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["grade", "add-no-match", "test-issue-001", "Novel finding not in canonical ground truth"]
        )
        assert_that(result, exited_successfully())
        assert "Added no-match decision" in result.stdout
        yield from m.docker_exec_roundtrip(["grade", "submit", "Graded 1 issue: 1 novel finding"])

    return mock


@pytest.mark.timeout(180)  # 3 minutes for full 3-agent workflow
@pytest.mark.requires_docker
async def test_three_agent_workflow_with_grader_data_access(
    synced_test_db: DatabaseConfig, async_docker_client, test_snapshot, noop_openai_client, test_registry
):
    """Test complete 3-agent workflow: optimizer → critic → grader with data access verification."""
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
        grader_client=noop_openai_client,
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

    # Run grader with mock that verifies data access
    grader_mock = make_grader_mock_with_data_access(critic_run_id)
    grader_client = CapturingOpenAIModel(grader_mock)

    try:
        grader_run_id = await test_registry.run_grader(critic_run_id=critic_run_id, client=grader_client, max_turns=100)
        assert grader_run_id is not None

        # Verify grader completed successfully
        with get_session() as session:
            grader_run = session.get(AgentRun, grader_run_id)
            assert grader_run is not None
            assert grader_run.status == AgentRunStatus.COMPLETED, f"Expected COMPLETED, got {grader_run.status}"
            assert grader_run.grader_config().graded_agent_run_id == critic_run_id

            # Verify grading edge was written
            edges = session.query(GradingEdge).filter(GradingEdge.grader_run_id == grader_run_id).all()
            assert len(edges) == 1, f"Expected 1 edge, got {len(edges)}"
            edge = edges[0]
            assert edge.critique_issue_id == "test-issue-001"
            assert edge.tp_id is None  # no-match edge has NULL tp_id
            assert edge.fp_id is None  # no-match edge has NULL fp_id

    except (RuntimeError, AssertionError):
        # Print captured requests for debugging
        print(f"\n=== Captured {len(grader_client.captured)} grader requests ===")
        for i, req in enumerate(grader_client.captured):
            print(f"\n--- Request {i + 1} ---")
            if isinstance(req.input, list):
                for msg in req.input:
                    msg_dict = msg.model_dump()
                    role = msg_dict.get("role", str(type(msg).__name__))
                    content_preview = str(msg_dict)[:500]
                    print(f"  {role}: {content_preview}")
            elif isinstance(req.input, str):
                print(f"  (string input): {req.input[:200]}")
        raise


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
        grader_client=noop_openai_client,
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
        grader_client=noop_openai_client,
        docker_client=async_docker_client,
        target_metric=TargetMetric.WHOLE_REPO,
        db_config=synced_test_db,
    )
