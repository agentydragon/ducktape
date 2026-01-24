"""MCP tests for policy validation.

Tests that PolicyEngine rejects policies with failing or missing tests.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.client import Client

from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.testing.approval_policy_testdata import fetch_policy


@pytest.fixture
async def policy_engine(sqlite_persistence, async_docker_client) -> PolicyEngine:
    """PolicyEngine instance for validation tests."""
    return PolicyEngine(
        agent_id="testagent",
        persistence=sqlite_persistence,
        policy_source="# placeholder",
        docker_client=async_docker_client,
    )


@pytest.fixture
def failing_policy() -> str:
    """Policy source with failing tests."""
    result: str = fetch_policy("failing_tests")
    return result


@pytest.mark.requires_docker
class TestPolicyValidation:
    """Tests for policy validation via MCP admin tools."""

    async def test_set_policy_rejects_failing_tests(self, policy_engine, failing_policy):
        """Setting policy with failing tests raises an error."""
        async with Client(policy_engine.admin) as sess:
            result = await sess.call_tool("set_policy", {"source": failing_policy}, raise_on_error=False)
            assert result.is_error, "Expected error for failing tests policy"

    async def test_set_policy_accepts_valid_policy(self, policy_engine, policy_allow_all):
        """Setting valid policy succeeds."""
        async with Client(policy_engine.admin) as sess:
            result = await sess.call_tool("set_policy", {"source": policy_allow_all})
            assert not result.is_error

    async def test_create_proposal_validates_policy(self, policy_engine, failing_policy):
        """Creating proposal with failing tests returns error."""
        async with Client(policy_engine.proposer) as sess:
            result = await sess.call_tool("create_proposal", {"content": failing_policy}, raise_on_error=False)
            assert result.is_error, "Expected error for policy with failing tests"

    async def test_self_check_directly(self, policy_engine, failing_policy):
        """PolicyEngine.self_check raises for invalid policy."""
        with pytest.raises(RuntimeError, match="policy eval failed"):
            await policy_engine.self_check(failing_policy)

    async def test_self_check_passes_valid(self, policy_engine, policy_allow_all):
        """PolicyEngine.self_check passes for valid policy."""
        await policy_engine.self_check(policy_allow_all)


if __name__ == "__main__":
    pytest_bazel.main()
