from __future__ import annotations

import pytest
from tests.llm.support.openai_mock import FakeOpenAIModel

from adgn.agent.agent import MiniCodex
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.approval_policy.server import ApprovalPolicyServer


@pytest.mark.requires_docker
async def test_approval_policy_server_is_available(
    responses_factory, make_echo_spec, make_pg_compositor, approval_engine
):
    """Test that the approval policy MCP server is available to the agent and lists tools."""

    # Add approval server to specs
    reader = ApprovalPolicyServer(approval_engine)
    from adgn.mcp.approval_policy.server import ApprovalPolicyProposerServer

    proposer = ApprovalPolicyProposerServer(engine=approval_engine)
    servers = dict(make_echo_spec())
    servers["approval_policy"] = reader
    servers["approval_policy.proposer"] = proposer
    async with make_pg_compositor(servers) as (mcp_client, _comp):
        # Create a sequence where agent lists available tools
        seq = [responses_factory.make_assistant_message("I can see the approval tools")]
        client = FakeOpenAIModel(seq)

        # With servers attached, proceed with assertions
        # Check that approval_policy server is available and lists flat tools
        # List tools via a direct Compositor client
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        expected = {
            build_mcp_function("approval_policy.proposer", "create_proposal"),
            build_mcp_function("approval_policy.proposer", "withdraw_proposal"),
        }
        assert expected <= tool_names

        agent = await MiniCodex.create(
            model=responses_factory.model, mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        # Run should complete without issues
        result = await agent.run("test")
        assert "approval" in result.text.lower()
