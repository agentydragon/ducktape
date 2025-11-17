"""Integration test to verify approval system is wired correctly."""

import asyncio
from collections.abc import Callable

import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.approvals import ApprovalHub
from adgn.agent.handler import ContinueDecision
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from tests.llm.support.openai_mock import FakeOpenAIModel


@pytest.mark.requires_docker
async def test_approval_system_wired_and_blocks_on_ask(
    responses_factory,
    make_echo_spec,
    policy_make: Callable[..., str],
    make_pg_compositor,
    approval_hub: ApprovalHub,
    make_policy_engine,
) -> None:
    """Test that the approval system is properly wired and blocks tool calls via middleware."""

    # Prepare approval engine with an ASK policy for echo.echo using shared factory
    engine = make_policy_engine(
        policy_make(decision_expr="PolicyDecision.ASK", server="echo", tool="echo", default="ask")
    )

    # Model tries to call the tool then returns text
    seq = [
        responses_factory.make(responses_factory.tool_call(build_mcp_function("echo", "echo"), {"text": "test"})),
        responses_factory.make_assistant_message("done"),
    ]
    client = FakeOpenAIModel(seq)

    # Approval reader server for middleware evaluation
    from adgn.mcp.approval_policy.server import ApprovalPolicyServer

    reader = ApprovalPolicyServer(engine)
    servers = dict(make_echo_spec())
    servers["approval_policy"] = reader
    async with make_pg_compositor(servers, notifier=None) as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model=responses_factory.model, mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        # Start the agent run in the background
        run_task = asyncio.create_task(agent.run("test"))

        # Wait briefly for the agent to hit the approval block
        for _ in range(20):  # up to ~1s
            if len(approval_hub._requests) >= 1:
                break
            await asyncio.sleep(0.05)
        pending = approval_hub._requests
        assert len(pending) == 1, f"Expected 1 pending approval, got {len(pending)}"

        # Get the call_id from the pending approval
        call_id = next(iter(pending.keys()))

        # Approve the tool call; agent should now complete
        approval_hub.resolve(call_id, ContinueDecision())
        result = await run_task
        assert result.text.strip() == "done"


# Note: server availability and resources are tested under tests/mcp/approval_policy
