"""Tests for the approval policy MCP server surface: schemas, resources, and availability."""

from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.responses import DecoratorMock
from agent_server.mcp.approval_policy.engine import PolicyReaderServer
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_utils.resources import extract_single_text_content
from openai_utils.model import UserMessage


async def test_tool_schemas(make_typed_mcp, approval_policy_server):
    """Verify approval_policy proposer tools are exposed with flat typed schemas."""
    async with make_typed_mcp(approval_policy_server.proposer) as (client, _sess):
        names = set(client.models.keys())
        assert {"create_proposal", "withdraw_proposal"} <= names


async def test_resources_list_and_read_policy(make_typed_mcp, approval_policy_server):
    """List and read resources directly from the reader server."""
    server = approval_policy_server.reader

    async with make_typed_mcp(server) as (_, sess):
        items = await sess.list_resources()
        assert isinstance(items, list)
        policy_resource = next((r for r in items if r.name == "policy.py"), None)
        assert policy_resource is not None
        assert str(policy_resource.uri) == str(PolicyReaderServer.ACTIVE_POLICY_URI)
        assert policy_resource.mimeType == "text/x-python"

        result = await sess.read_resource(PolicyReaderServer.ACTIVE_POLICY_URI)
        policy_text = extract_single_text_content(result)
        assert "def decide" in policy_text
        assert "PolicyResponse" in policy_text


async def test_server_available_in_compositor(echo_spec, make_policy_gateway_compositor):
    """Test that the approval policy MCP server is available to the agent and lists tools."""

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        yield m.assistant_text("I can see the approval tools")

    servers = dict(echo_spec)
    async with make_policy_gateway_compositor(servers) as comp, Client(comp) as mcp_client:
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        expected = {
            build_mcp_function(MCPMountPrefix("policy_proposer"), "create_proposal"),
            build_mcp_function(MCPMountPrefix("policy_proposer"), "withdraw_proposal"),
        }
        assert expected <= tool_names

        agent = await Agent.create(
            tool_provider=MCPToolProvider(mcp_client),
            client=mock,
            handlers=[FinishOnTextMessageHandler()],
            tool_policy=AllowAnyToolOrTextMessage(),
        )
        agent.process_message(UserMessage.text("test"))

        result = await agent.run()
        assert "approval" in result.text.lower()


if __name__ == "__main__":
    pytest_bazel.main()
