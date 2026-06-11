"""Test prompt improvement agent end-to-end with mocked OpenAI.

Tests the improvement agent workflow using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from PropsMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → PropsMock

Tests verify:
- Creating improved package directory via docker_exec
- Database access works from container
- CLI helpers work in container context

Note: These tests terminate via report-failure since actual termination
condition checks would require real grading infrastructure.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import all_of, assert_that

from agent_core_testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully, stdout_contains
from props.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.db.examples import Example
from props.db.models import AgentRun
from props.db.session import get_session
from props.testing.mocks import PropsMock

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

# Test timeout (seconds) - applies to container execution
TEST_TIMEOUT_SECONDS = 120

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
from props.db.session import get_session
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
    """Create mock for improvement agent that creates files and terminates."""

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
        # Terminate via report-failure (real termination requires grading infrastructure)
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Package created, test complete"])

    return mock


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_prompt_improve_e2e_creates_package(e2e_stack, subtract_file_example):
    """Test improvement agent can create package directory in container."""
    mock = make_improvement_mock()

    async with e2e_stack(mock) as stack:
        result = await stack.registry.run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    # Agent terminated via report-failure, so run_id should be valid
    assert result.run_id is not None

    with get_session() as session:
        agent_run = session.query(AgentRun).filter_by(agent_run_id=result.run_id).one()
        improvement_config = agent_run.improvement_config()
        assert improvement_config.agent_type == "improvement"
        assert improvement_config.allowed_examples is not None


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_prompt_improve_e2e_multiple_examples(e2e_stack, test_snapshot):
    """Test improvement agent with multiple training examples."""
    with get_session() as session:
        examples = session.query(Example).filter_by(snapshot_slug=test_snapshot).limit(2).all()
        assert len(examples) >= 2, "Need at least 2 examples for this test"
        allowed_examples = [e.to_example_spec() for e in examples]

    mock = make_improvement_mock()

    async with e2e_stack(mock) as stack:
        result = await stack.registry.run_improvement_agent(
            examples=allowed_examples,
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    assert result.run_id is not None

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
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Leaderboard test completed"])

    return mock


def make_hard_examples_check_mock() -> PropsMock:
    """Create mock that runs hard-examples and terminates."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(["critic-dev", "hard-examples", "--limit", "5"], timeout_ms=30000)
        assert_that(result, all_of(exited_successfully(), stdout_contains("76%")))
        yield from m.docker_exec_roundtrip(["critic-dev", "report-failure", "Hard examples test completed"])

    return mock


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_cli_leaderboard_in_improvement_agent(e2e_stack, subtract_file_example, test_train_example_with_runs):
    """Test that leaderboard CLI command works from improvement agent container."""
    mock = make_leaderboard_check_mock()

    async with e2e_stack(mock) as stack:
        result = await stack.registry.run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    assert result.run_id is not None


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_cli_hard_examples_in_improvement_agent(e2e_stack, subtract_file_example, test_train_example_with_runs):
    """Test that hard-examples CLI command works from improvement agent container."""
    mock = make_hard_examples_check_mock()

    async with e2e_stack(mock) as stack:
        result = await stack.registry.run_improvement_agent(
            examples=[subtract_file_example],
            baseline_image_refs=[CRITIC_IMAGE_REF],
            token_budget=100_000,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    assert result.run_id is not None


if __name__ == "__main__":
    pytest_bazel.main()
