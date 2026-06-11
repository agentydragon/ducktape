"""Test that approval policy MCP server exposes proper tool schemas."""

import pytest
import pytest_bazel


@pytest.mark.requires_docker
async def test_approval_policy_tool_schemas(make_typed_mcp, approval_policy_server):
    """Verify approval_policy tools are exposed with flat typed schemas."""

    # policy_server owns .proposer sub-server with the proposer tools
    async with make_typed_mcp(approval_policy_server.proposer) as (client, _sess):
        # Expect typed tools available
        names = set(client.models.keys())
        assert {"create_proposal", "withdraw_proposal"} <= names


if __name__ == "__main__":
    pytest_bazel.main()
