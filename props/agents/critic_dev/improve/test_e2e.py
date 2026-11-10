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

import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen
from mcp_infra.testing.matchers import exited_successfully
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.core.agent_types import CriticDevImproveTypeConfig
from props.db.database import Database
from props.db.models import AgentRun
from props.testing.constants import DEFAULT_TEST_MODEL

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
# gazelle:include_dep //props/testing/fixtures:scopes
# gazelle:include_dep //mcp_infra/testing:fixtures
# gazelle:include_dep //props/testing/fixtures:e2e_infra
# gazelle:include_dep //props/testing/fixtures:e2e_container
pytest_plugins = (
    "util.testing.postgres_fixtures",
    "props.testing.fixtures.db",
    "props.testing.fixtures.scopes",
    "mcp_infra.testing.fixtures",
    "props.testing.fixtures.e2e_infra",
    "props.testing.fixtures.e2e_container",
)

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
        # The image entrypoint initialized the aspect_rules_py venv. A bare
        # child `python3` must inherit that venv's first_party.pth so imports
        # work without PYTHONPATH knowing Bazel's runfiles repository name.
        result = yield from m.exec_roundtrip(
            ["python3", "-c", "from props.db.database import Database"], timeout_ms=15000
        )
        assert_that(result, exited_successfully())

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


async def test_prompt_improve_e2e_creates_package(
    e2e_stack, subtract_file_example, critic_dev_improve_image, critic_image, db: Database
):
    """Test improvement agent can create package directory in container."""
    mock = make_improvement_mock()

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_dev_improve_image, critic_image]) as stack:
        result = await stack.registry.run_critic_dev_improve(
            image=stack.resolved_images["critic_dev_improve"],
            examples=[subtract_file_example],
            baseline_image_digests=[stack.image_digests["critic"]],
            budget_usd=50.0,
            improvement_model=stack.model,
            critic_model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
        )

    with db.session() as session:
        agent_run = session.query(AgentRun).filter_by(agent_run_id=result).one()
        assert isinstance(agent_run.type_config, CriticDevImproveTypeConfig)
        assert agent_run.type_config.allowed_examples == [subtract_file_example]


if __name__ == "__main__":
    pytest_bazel.main()
