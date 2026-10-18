"""Dual mock/live tests for an LLM-driven Docker exec roundtrip.

Both need Docker (the exec tool runs in a real container).
"""

from __future__ import annotations

import pytest_bazel
from fastmcp.client import Client

from agent_core.agent import Agent, AgentResult
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.handlers import FinishOnTextMessageHandler
from agent_core.testing.responses import DecoratorMock, tool_roundtrip
from openai_utils.model import UserMessage
from util.exec.models import BaseExecResult, Exited

ECHO_CMD = ["/bin/echo", "-n", "hello"]


async def test_llm_exec_echo(mock_or_live, docker_exec_server) -> None:
    """LLM calls the exec tool to echo hello, and the agent returns stdout."""

    @mock_or_live(DecoratorMock)
    def client(m: DecoratorMock):
        yield
        call = m.tool_call("exec", {"cmd": ECHO_CMD, "timeout_ms": 10000, "env": None, "user": None, "cwd": None})
        result: BaseExecResult = yield from tool_roundtrip(call, BaseExecResult)
        assert isinstance(result.exit, Exited)
        assert result.exit.exit_code == 0
        assert (result.stdout or "") == "hello"
        yield m.assistant_text("hello")

    async with Client(docker_exec_server) as mcp_client:
        agent = await Agent.create(
            tool_provider=MCPToolProvider(mcp_client),
            client=client,
            # Simple echo roundtrip shouldn't need more than ~3 turns; 10 is a generous safety margin.
            handlers=[FinishOnTextMessageHandler()],
            tool_policy=AllowAnyToolOrTextMessage(),
        )
        agent.process_message(
            UserMessage.text(f"Call the exec tool with cmd={ECHO_CMD!r} and return exactly the stdout.")
        )
        res: AgentResult = await agent.run()
        assert "hello" in (res.text or "")


if __name__ == "__main__":
    pytest_bazel.main()
