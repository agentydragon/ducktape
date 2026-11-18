"""Tests for MCP tool integration with the agent."""

from __future__ import annotations

import pytest

from openai_utils.model import SystemMessage, UserMessage
from skills.info_gathering.evals.agent_runtime.agent import Agent
from skills.info_gathering.evals.agent_runtime.events import ToolCallOutput
from skills.info_gathering.evals.agent_runtime.loop_control import RequireAnyTool
from skills.info_gathering.evals.agent_runtime.testing.handlers import TextCaptureHandler, run_until_text
from skills.info_gathering.evals.agent_runtime.testing.mcp.responses import EchoMock


@pytest.mark.parametrize(
    "message", [SystemMessage.text("test: use echo"), UserMessage.text("say hello")], ids=["system", "user"]
)
async def test_agent_mcp_echo(mcp_tool_provider_echo, message) -> None:
    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hello")
        yield m.assistant_text("done")

    capture = TextCaptureHandler()
    agent = Agent(tool_provider=mcp_tool_provider_echo, client=mock, handlers=[capture], tool_policy=RequireAnyTool())
    agent.process_message(message)

    res = await run_until_text(agent, capture)

    outputs = [r for r in agent._transcript if isinstance(r, ToolCallOutput)]
    assert outputs, "No tool outputs captured"
    assert outputs[0].result.structured_content == {"echo": "hello"}
    assert res.strip() == "done"
