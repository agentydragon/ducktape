"""Integration test to verify approval system is wired correctly."""

import asyncio

import pytest
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core_testing.responses import EchoMock
from agent_server.mcp.approval_policy.engine import CallDecision, PendingCallsResponse
from agent_server.policies.policy_types import ApprovalDecision
from agent_server.testing.approval_policy_testdata import make_policy
from mcp_infra.resource_utils import read_text_json_typed
from openai_utils.model import SystemMessage


@pytest.mark.requires_docker
@pytest.mark.requires_runtime_image
async def test_approval_system_wired_and_blocks_on_ask(
    echo_spec, make_policy_gateway_compositor, make_approval_policy_server
) -> None:
    """Test that the approval system is properly wired and blocks tool calls via middleware."""

    # Prepare approval engine with an ASK policy for echo.echo using shared factory
    engine = await make_approval_policy_server(
        make_policy(decision_expr="ApprovalDecision.ASK", server="echo", tool="echo", default=ApprovalDecision.ASK)
    )

    # Model tries to call the tool once (needs approval) then finishes with text
    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield m.echo_call("test")
        yield m.assistant_text("done")

    # Use make_policy_gateway_compositor with custom policy engine
    servers = dict(echo_spec)
    async with make_policy_gateway_compositor(servers, policy_engine=engine) as comp, Client(comp) as mcp_client:
        agent = await Agent.create(
            mcp_client=mcp_client,
            client=mock,
            handlers=[FinishOnTextMessageHandler()],
            tool_policy=AllowAnyToolOrTextMessage(),
        )
        agent.process_message(SystemMessage.text("test"))

        # Start the agent run in the background
        run_task = asyncio.create_task(agent.run())

        # Give the agent time to start and hit the approval block
        await asyncio.sleep(0.5)

        # Wait briefly for the agent to hit the approval block
        # Read pending://calls resource from reader server via MCP
        async with Client(comp._approval_engine.reader) as reader_client:
            pending_data: PendingCallsResponse
            for _ in range(20):  # up to ~1s
                pending_data = await read_text_json_typed(
                    reader_client, comp._approval_engine.reader.pending_calls_resource.uri, PendingCallsResponse
                )
                if len(pending_data.pending) >= 1:
                    break
                await asyncio.sleep(0.05)

            assert len(pending_data.pending) == 1, f"Expected 1 pending approval, got {len(pending_data.pending)}"

            # Get the call_id from the pending approval
            call_id = pending_data.pending[0].call_id

        # Approve the tool call via admin server's decide_call tool
        async with Client(comp._approval_engine.admin) as admin_client:
            await admin_client.call_tool(
                "decide_call", arguments={"call_id": call_id, "decision": CallDecision.APPROVE}
            )
        result = await run_task
        assert result.text.strip() == "done"


# Note: server availability and resources are tested under tests/mcp/approval_policy
