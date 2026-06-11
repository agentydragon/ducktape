"""Test grader agent with HTTP MCP mode (MCP-over-HTTP with bearer token auth).

Tests the new HTTP transport for grader_submit tools using real Docker containers,
real PostgreSQL database, and mocked OpenAI responses.

Comprehensive tests verify:
- Reading critic runs (reported_issues from the critique being graded)
- Reading ground truth (true_positives, false_positives)
- Writing grading decisions
- Submitting via MCP
"""

from __future__ import annotations

from uuid import UUID

import pytest
from hamcrest import assert_that

from agent_core_testing.openai_mock import CapturingOpenAIModel
from agent_core_testing.responses import PlayGen
from agent_core_testing.steps import exited_successfully
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.models import AgentRun, AgentRunStatus, GradingEdge
from props.core.db.session import get_session
from props.testing.mocks import PropsMock


def make_critic_mock_zero_issues() -> PropsMock:
    """Create mock for critic that finds zero issues."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Reviewed code, no issues found"])

    return mock


@pytest.fixture
async def zero_issue_critic_run(test_registry, all_files_scope):
    """Create a zero-issue critic run for grader testing."""
    mock = make_critic_mock_zero_issues()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
    )
    assert critic_run_id is not None
    return critic_run_id


def make_grader_mock_zero_issues() -> PropsMock:
    """Create mock for grader that grades zero-issue critique."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        yield from m.docker_exec_roundtrip(
            ["grade", "submit", "Graded zero-issue critique against ground truth. No issues to match."]
        )

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_grader_http_mode_zero_issues(zero_issue_critic_run, test_snapshot, test_registry):
    """Test grader successfully grades zero-issue critique using HTTP MCP mode."""
    mock = make_grader_mock_zero_issues()
    grader_client = CapturingOpenAIModel(mock)

    try:
        grader_run_id = await test_registry.run_grader(
            critic_run_id=zero_issue_critic_run, client=grader_client, max_turns=100
        )

        assert grader_run_id is not None

        with get_session() as session:
            grader_run = session.get(AgentRun, grader_run_id)
            assert grader_run is not None
            grader_config = grader_run.grader_config()
            graded_critic = session.get(AgentRun, grader_config.graded_agent_run_id)
            assert graded_critic is not None
            assert graded_critic.critic_config().example.snapshot_slug == test_snapshot
            assert grader_config.graded_agent_run_id == zero_issue_critic_run
            assert grader_run.status == AgentRunStatus.COMPLETED, f"Expected COMPLETED, got {grader_run.status}"
    except (RuntimeError, AssertionError):
        print(f"\n=== Captured {len(grader_client.captured)} requests ===")
        for i, req in enumerate(grader_client.captured):
            print(f"\n--- Request {i + 1} ---")
            if isinstance(req.input, list):
                for msg in req.input:
                    msg_dict = msg.model_dump()
                    role = msg_dict.get("role", str(type(msg).__name__))
                    content_preview = str(msg_dict)[:200]
                    print(f"  {role}: {content_preview}")
            elif isinstance(req.input, str):
                print(f"  (string input): {req.input[:200]}")
        raise


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_grader_http_mode_sql_workflow(zero_issue_critic_run, test_registry):
    """Test grader HTTP mode with SQL workflow."""
    mock = make_grader_mock_zero_issues()
    grader_run_id = await test_registry.run_grader(critic_run_id=zero_issue_critic_run, client=mock, max_turns=100)

    assert grader_run_id is not None

    with get_session() as session:
        grader_run = session.get(AgentRun, grader_run_id)
        assert grader_run is not None
        assert grader_run.status == AgentRunStatus.COMPLETED


# =============================================================================
# Comprehensive Data Access Test
# =============================================================================


def make_critic_mock_with_issue() -> PropsMock:
    """Create mock for critic that submits one issue."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-issue", "test-issue-01", "Test issue found in code"]
        )
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "test-issue-01", "subtract.py", "-s", "1", "-e", "5"]
        )
        assert_that(result, exited_successfully())
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 test issue"])

    return mock


@pytest.fixture
async def critic_run_with_issue(test_registry, all_files_scope):
    """Create a critic run with one reported issue for grader testing."""
    mock = make_critic_mock_with_issue()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
    )
    assert critic_run_id is not None

    with get_session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        assert len(critic_run.reported_issues) == 1, f"Expected 1 issue, got {len(critic_run.reported_issues)}"

    return critic_run_id


def make_grader_mock_comprehensive(critic_run_id: UUID, fp_id: str) -> PropsMock:
    """Create mock for grader that demonstrates full data access."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        # Read reported_issues from the critique being graded
        result = yield from m.psql_roundtrip(
            f"SELECT issue_id, rationale FROM reported_issues WHERE agent_run_id = '{critic_run_id}'"
        )
        assert_that(result, exited_successfully())
        assert "test-issue-01" in result.stdout
        # Read true_positives
        result = yield from m.psql_roundtrip("SELECT tp_id, rationale FROM true_positives LIMIT 5")
        assert_that(result, exited_successfully())
        assert "test-issue" in result.stdout
        # Read false_positives
        result = yield from m.psql_roundtrip("SELECT fp_id, rationale FROM false_positives LIMIT 5")
        assert_that(result, exited_successfully())
        assert fp_id in result.stdout
        # Add no-match decision
        result = yield from m.docker_exec_roundtrip(
            ["grade", "add-no-match", "test-issue-01", "Novel finding not in canonical ground truth"]
        )
        assert_that(result, exited_successfully())
        assert "Added no-match decision" in result.stdout
        # Submit
        yield from m.docker_exec_roundtrip(["grade", "submit", "Graded 1 issue: 1 novel finding (no canonical match)"])

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
@pytest.mark.timeout(60)
async def test_grader_comprehensive_data_access(critic_run_with_issue, test_snapshot, test_registry, fp_id):
    """Test grader can read critic runs, ground truth, write decisions, and submit."""
    mock = make_grader_mock_comprehensive(critic_run_with_issue, fp_id)
    grader_client = CapturingOpenAIModel(mock)

    try:
        grader_run_id = await test_registry.run_grader(
            critic_run_id=critic_run_with_issue, client=grader_client, max_turns=100
        )

        assert grader_run_id is not None

        with get_session() as session:
            grader_run = session.get(AgentRun, grader_run_id)
            assert grader_run is not None
            grader_config2 = grader_run.grader_config()
            graded_critic = session.get(AgentRun, grader_config2.graded_agent_run_id)
            assert graded_critic is not None
            assert graded_critic.critic_config().example.snapshot_slug == test_snapshot
            assert grader_config2.graded_agent_run_id == critic_run_with_issue
            assert grader_run.status == AgentRunStatus.COMPLETED, f"Expected COMPLETED, got {grader_run.status}"

            # Verify grading edge was written
            edges = session.query(GradingEdge).filter(GradingEdge.grader_run_id == grader_run_id).all()
            assert len(edges) == 1, f"Expected 1 edge, got {len(edges)}"
            edge = edges[0]
            assert edge.critique_issue_id == "test-issue-01"
            assert edge.tp_id is None  # no-match edge has NULL tp_id
            assert edge.fp_id is None  # no-match edge has NULL fp_id

    except (RuntimeError, AssertionError):
        print(f"\n=== Captured {len(grader_client.captured)} requests ===")
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
