from __future__ import annotations

import pytest
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core_testing.responses import DecoratorMock
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import UserMessage


@pytest.mark.requires_docker
async def test_approval_policy_server_is_available(echo_spec, make_policy_gateway_compositor):
    """Test that the approval policy MCP server is available to the agent and lists tools."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        yield m.assistant_text("I can see the approval tools")

    # make_policy_gateway_compositor creates a PolicyEngine with all servers (reader, proposer, admin) already mounted
    servers = dict(echo_spec)
    async with make_policy_gateway_compositor(servers) as comp, Client(comp) as mcp_client:
        # With servers attached, proceed with assertions
        # Check that policy servers are available and list flat tools
        # List tools via a direct Compositor client
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        expected = {
            build_mcp_function(MCPMountPrefix("policy_proposer"), "create_proposal"),
            build_mcp_function(MCPMountPrefix("policy_proposer"), "withdraw_proposal"),
        }
        assert expected <= tool_names

        agent = await Agent.create(
            mcp_client=mcp_client,
            client=mock,
            handlers=[FinishOnTextMessageHandler()],
            tool_policy=AllowAnyToolOrTextMessage(),
        )
        agent.process_message(UserMessage.text("test"))

        # Run should complete without issues
        result = await agent.run()
        assert "approval" in result.text.lower()
