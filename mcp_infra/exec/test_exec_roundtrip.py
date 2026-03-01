"""Dual mock/live tests for LLM-driven Docker exec roundtrip.

Mock test scripts the LLM to call box__exec; live test uses a real OpenAI model.
Both need Docker (the exec tool runs in a real container).
"""

from __future__ import annotations

import logging

import pytest_bazel

from agent_core.agent import Agent, AgentResult
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.mcp.responses import MCPDecoratorMock
from agent_core.testing.responses import tool_roundtrip
from agent_core.turn_limit import MaxTurnsHandler
from mcp_infra.exec.models import BaseExecResult, Exited, make_exec_input
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import UserMessage

logger = logging.getLogger(__name__)

ECHO_CMD = ["/bin/echo", "-n", "hello"]
SERVER_NAME = MCPMountPrefix("box")


async def test_llm_exec_echo(mock_or_live, docker_exec_server, compositor, compositor_client) -> None:
    """LLM calls box__exec to echo hello, agent returns stdout."""
    await compositor.mount_inproc(MCPMountPrefix("box"), docker_exec_server)

    @mock_or_live(MCPDecoratorMock)
    def client(m: MCPDecoratorMock):
        yield
        call = m.mcp_tool_call(SERVER_NAME, "exec", make_exec_input(ECHO_CMD))
        result: BaseExecResult = yield from tool_roundtrip(call, BaseExecResult)
        assert isinstance(result.exit, Exited)
        assert result.exit.exit_code == 0
        assert (result.stdout or "") == "hello"
        yield m.assistant_text("hello")

    agent = await Agent.create(
        tool_provider=MCPToolProvider(compositor_client),
        client=client,
        # Simple echo roundtrip shouldn't need more than ~3 turns; 10 is a generous safety margin.
        handlers=[FinishOnTextMessageHandler(), MaxTurnsHandler(max_turns=10), LoggingHandler(logger)],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text(f"Call the exec tool with cmd={ECHO_CMD!r} and return exactly the stdout."))
    res: AgentResult = await agent.run()
    assert "hello" in (res.text or "")


if __name__ == "__main__":
    pytest_bazel.main()
