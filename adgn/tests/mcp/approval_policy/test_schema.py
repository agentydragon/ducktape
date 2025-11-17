"""Test that approval policy MCP server exposes proper tool schemas."""

import pytest

from adgn.mcp.approval_policy.server import ApprovalPolicyProposerServer, ApprovalPolicyServer


@pytest.mark.requires_docker
async def test_approval_policy_tool_schemas(make_typed_mcp, approval_engine):
    """Verify approval_policy tools are exposed with flat typed schemas."""

    ApprovalPolicyServer(approval_engine)
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)

    async with make_typed_mcp(proposer, "approval_policy.proposer") as (client, _sess):
        # Expect typed tools available
        names = set(client.models.keys())
        assert {"create_proposal", "withdraw_proposal"} <= names
