"""Tests for MCP tool integration with the agent."""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import has_entries

from agent_core.agent import Agent
from agent_core.loop_control import RequireAnyTool
from agent_core.testing.matchers import assert_function_call_output_structured
from agent_core.testing.mcp.responses import EchoMock
from openai_utils.model import SystemMessage, UserMessage


@pytest.mark.parametrize(
    ("message", "agent_options"),
    [
        pytest.param(SystemMessage.text("test: use echo"), {"parallel_tool_calls": False}, id="system-message"),
        pytest.param(UserMessage.text("say hello"), {}, id="user-message-default-options"),
    ],
)
async def test_agent_mcp_echo(mcp_tool_provider_echo, test_handlers, recording_handler, message, agent_options) -> None:
    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hello")
        yield m.assistant_text("done")

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo,
        client=mock,
        handlers=test_handlers,
        tool_policy=RequireAnyTool(),
        **agent_options,
    )
    agent.process_message(message)

    res = await agent.run()

    assert_function_call_output_structured(recording_handler.records, has_entries(echo="hello"))
    outputs = [r for r in recording_handler.records if r.type == "function_call_output"]
    assert outputs, "No tool outputs captured"
    assert outputs[0].result.structured_content == {"echo": "hello"}
    assert res.text.strip() == "done"


if __name__ == "__main__":
    pytest_bazel.main()
