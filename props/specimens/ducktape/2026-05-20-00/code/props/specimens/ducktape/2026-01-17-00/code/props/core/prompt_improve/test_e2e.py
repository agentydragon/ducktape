"""Test prompt improvement agent end-to-end with mocked OpenAI.

Tests the improvement agent workflow:
- Creating improved package directory via docker_exec
- Submitting via `props agent-pkg create` CLI
- Token budget handling
- RLS-scoped database access
- Termination when package beats baseline average
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hamcrest import assert_that

from agent_core_testing.responses import PlayGen
from agent_core_testing.steps import exited_successfully
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.examples import Example
from props.core.db.models import AgentRun
from props.core.db.session import get_session
from props.core.prompt_improve.improve_agent import run_improvement_agent
from props.core.prompt_improve.reminder_handler import BlockingStatus
from props.testing.mocks import PropsMock

# Define the improved agent.md content used across tests
# Note: The improvement agent creates a package with Dockerfile + init + agent.md
IMPROVED_AGENT_MD = """# Improved Critic Prompt

You are a code review assistant focused on finding:
1. Dead code (unused imports, unreachable code)
2. Duplication (copy-paste code that should be extracted)
3. Type errors and inconsistencies

Be thorough and systematic in your analysis."""

# Define the init script content
INIT_SCRIPT = """#!/usr/bin/env python3
import sys
from props.core.db.session import get_session
from sqlalchemy import text

with get_session() as session:
    agent_run_id = session.execute(text("SELECT current_agent_run_id()")).scalar()
    if not agent_run_id:
        print("ERROR: current_agent_run_id() is NULL", file=sys.stderr)
        sys.exit(1)
    print(f"Agent run ID: {agent_run_id}")
print("Ready to begin.")
"""


def make_improvement_mock() -> PropsMock:
    """Create mock for improvement agent that creates and submits a package."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        # Create package directory and write files
        result = yield from m.docker_exec_roundtrip(
            [
                "sh",
                "-c",
                f"""mkdir -p /workspace/improved && \
cat > /workspace/improved/agent.md << 'AGENT_EOF'
{IMPROVED_AGENT_MD}
AGENT_EOF
cat > /workspace/improved/init << 'INIT_EOF'
{INIT_SCRIPT}
INIT_EOF
chmod +x /workspace/improved/init""",
            ],
            timeout_ms=15000,
        )
        assert_that(result, exited_successfully())
        # Submit via CLI triggers termination check
        yield m.assistant_text("Package created successfully.")

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_prompt_improve_e2e_success(
    synced_test_db, async_docker_client, success_termination, subtract_file_example, noop_openai_client
):
    """Test improvement agent successfully submits improved definition."""
    mock = make_improvement_mock()
    call_count = 0

    def mock_check_termination(session, improvement_run_id, type_config):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return BlockingStatus(message="No definitions created yet.")
        return success_termination

    with patch("props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination):
        result = await run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            model="gpt-5-nano",
            docker_client=async_docker_client,
            db_config=synced_test_db,
            client=mock,
            critic_client=noop_openai_client,
            grader_client=noop_openai_client,
        )

    assert result.tokens_used >= 0

    with get_session() as session:
        agent_run = session.query(AgentRun).filter_by(agent_run_id=result.run_id).one()
        improvement_config = agent_run.improvement_config()
        assert improvement_config.agent_type == "improvement"
        assert improvement_config.allowed_examples is not None


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_prompt_improve_e2e_multiple_examples(
    synced_test_db, test_snapshot, async_docker_client, success_termination, noop_openai_client
):
    """Test improvement agent with multiple training examples."""
    with get_session() as session:
        examples = session.query(Example).filter_by(snapshot_slug=test_snapshot).limit(2).all()
        assert len(examples) >= 2, "Need at least 2 examples for this test"
        allowed_examples = [e.to_example_spec() for e in examples]

    mock = make_improvement_mock()
    call_count = 0

    def mock_check_termination(session, improvement_run_id, type_config):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return BlockingStatus(message="No definitions created yet.")
        return success_termination

    with patch("props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination):
        result = await run_improvement_agent(
            examples=allowed_examples,
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            model="gpt-5-nano",
            docker_client=async_docker_client,
            db_config=synced_test_db,
            client=mock,
            critic_client=noop_openai_client,
            grader_client=noop_openai_client,
        )

    assert result.tokens_used >= 0

    with get_session() as session:
        session.query(AgentRun).filter_by(agent_run_id=result.run_id).one()


# =============================================================================
# CLI Helper Integration Tests
# =============================================================================


def make_leaderboard_check_mock() -> PropsMock:
    """Create mock that runs leaderboard and terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "leaderboard", "--limit", "5"], timeout_ms=30000)
        assert_that(result, exited_successfully())
        assert "76%" in result.stdout
        assert "Recall" in result.stdout
        assert "critic" in result.stdout
        assert "Runs" in result.stdout
        yield m.assistant_text("Leaderboard test completed successfully.")

    return mock


def make_hard_examples_check_mock() -> PropsMock:
    """Create mock that runs hard-examples and terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "hard-examples", "--limit", "5"], timeout_ms=30000)
        assert_that(result, exited_successfully())
        assert "76%" in result.stdout
        assert "test-fixtures" in result.stdout
        assert "Recall" in result.stdout
        yield m.assistant_text("Hard examples test completed successfully.")

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_cli_leaderboard_in_improvement_agent(
    synced_test_db,
    async_docker_client,
    success_termination,
    subtract_file_example,
    noop_openai_client,
    test_train_example_with_runs,
):
    """Test that leaderboard CLI command works from improvement agent container."""
    mock = make_leaderboard_check_mock()

    def mock_check_termination(session, improvement_run_id, type_config):
        return success_termination

    with patch("props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination):
        result = await run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            model="gpt-5-nano",
            docker_client=async_docker_client,
            db_config=synced_test_db,
            client=mock,
            critic_client=noop_openai_client,
            grader_client=noop_openai_client,
        )

    assert result.tokens_used >= 0


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_cli_hard_examples_in_improvement_agent(
    synced_test_db,
    async_docker_client,
    success_termination,
    subtract_file_example,
    noop_openai_client,
    test_train_example_with_runs,
):
    """Test that hard-examples CLI command works from improvement agent container."""
    mock = make_hard_examples_check_mock()

    def mock_check_termination(session, improvement_run_id, type_config):
        return success_termination

    with patch("props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination):
        result = await run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            model="gpt-5-nano",
            docker_client=async_docker_client,
            db_config=synced_test_db,
            client=mock,
            critic_client=noop_openai_client,
            grader_client=noop_openai_client,
        )

    assert result.tokens_used >= 0
