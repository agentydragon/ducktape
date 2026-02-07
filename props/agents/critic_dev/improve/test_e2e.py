"""Test prompt improvement agent end-to-end with mocked OpenAI.

Tests the improvement agent workflow using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from CriticDevMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → CriticDevMock

Tests verify:
- Creating improved package directory via subprocess exec
- Database access works from container
- CLI helpers work in container context

Note: These tests terminate via the report_failure tool since actual termination
condition checks would require real grading infrastructure.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully
from props.agents.critic_dev.testing.mocks import CriticDevMock, make_cli_test_mock
from props.db.database import Database
from props.db.examples import Example
from props.db.models import AgentRun
from props.testing.constants import DEFAULT_TEST_MODEL

pytestmark = [pytest.mark.integration]

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
from props.db.database import Database
from props.agents.runtime import get_current_agent_run_id

db = Database.from_env()
with db.session() as session:
    agent_run_id = get_current_agent_run_id(session)
    print(f"Agent run ID: {agent_run_id}")
print("Ready to begin.")
"""


def make_improvement_mock() -> CriticDevMock:
    """Create mock for improvement agent that creates files and terminates."""

    @CriticDevMock.mock()
    def mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request
        # Create package directory and write files
        result = yield from m.exec_roundtrip(
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
        # Terminate via report_failure tool (real termination requires grading infrastructure)
        yield m.report_failure("Package created, test complete")

    return mock


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_prompt_improve_e2e_creates_package(
    e2e_stack, subtract_file_example, critic_dev_improve_image, critic_image, db: Database
):
    """Test improvement agent can create package directory in container."""
    mock = make_improvement_mock()

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_dev_improve_image, critic_image]) as stack:
        result = await stack.registry.run_critic_dev_improve(
            examples=[subtract_file_example],
            baseline_image_digests=[stack.image_digests["critic"]],
            budget_usd=50.0,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    with db.session() as session:
        agent_run = session.query(AgentRun).filter_by(agent_run_id=result).one()
        improvement_config = agent_run.critic_dev_improve_config()
        assert improvement_config.agent_type == "critic_dev_improve"
        assert improvement_config.allowed_examples is not None


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_prompt_improve_e2e_multiple_examples(
    e2e_stack, test_snapshot, critic_dev_improve_image, critic_image, db: Database
):
    """Test improvement agent with multiple training examples."""
    with db.session() as session:
        examples = session.query(Example).filter_by(snapshot_slug=test_snapshot).limit(2).all()
        assert len(examples) >= 2, "Need at least 2 examples for this test"
        allowed_examples = [e.to_example_spec() for e in examples]

    mock = make_improvement_mock()

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_dev_improve_image, critic_image]) as stack:
        result = await stack.registry.run_critic_dev_improve(
            examples=allowed_examples,
            baseline_image_digests=[stack.image_digests["critic"]],
            budget_usd=50.0,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    with db.session() as session:
        session.query(AgentRun).filter_by(agent_run_id=result).one()


# =============================================================================
# CLI Helper Integration Tests
# =============================================================================


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_cli_leaderboard_in_improvement_agent(
    e2e_stack, subtract_file_example, test_train_example_with_runs, critic_dev_improve_image, critic_image
):
    """Test that leaderboard CLI command works from improvement agent container."""
    mock = make_cli_test_mock(["critic-dev", "leaderboard", "--limit", "5"], expected_output="76%")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_dev_improve_image, critic_image]) as stack:
        result = await stack.registry.run_critic_dev_improve(
            examples=[subtract_file_example],
            baseline_image_digests=[stack.image_digests["critic"]],
            budget_usd=50.0,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    assert result is not None


@pytest.mark.timeout(180)
@pytest.mark.requires_docker
async def test_cli_hard_examples_in_improvement_agent(
    e2e_stack, subtract_file_example, test_train_example_with_runs, critic_dev_improve_image, critic_image
):
    """Test that hard-examples CLI command works from improvement agent container."""
    mock = make_cli_test_mock(["critic-dev", "hard-examples", "--limit", "5"], expected_output="76%")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_dev_improve_image, critic_image]) as stack:
        result = await stack.registry.run_critic_dev_improve(
            examples=[subtract_file_example],
            baseline_image_digests=[stack.image_digests["critic"]],
            budget_usd=50.0,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    assert result is not None


if __name__ == "__main__":
    pytest_bazel.main()
