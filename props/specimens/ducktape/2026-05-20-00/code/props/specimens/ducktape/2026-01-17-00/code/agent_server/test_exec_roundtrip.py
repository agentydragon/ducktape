from __future__ import annotations

import os

import pytest

from agent_core.agent import Agent, AgentResult
from agent_core.handler import BaseHandler
from agent_core.loop_control import RequireAnyTool
from mcp_infra.exec.models import BaseExecResult, Exited, make_exec_input
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.stubs.typed_stubs import ToolStub
from openai_utils.client_factory import build_client
from openai_utils.model import SystemMessage, UserMessage

# Use /bin/echo -n for portability and to avoid trailing newline
ECHO_CMD = ["/bin/echo", "-n", "hello"]

SERVER_NAME = MCPMountPrefix("box")


async def _assert_exec_echo(sess) -> None:
    # Call via compositor using namespaced tool key
    stub = ToolStub(sess, build_mcp_function(SERVER_NAME, "exec"), BaseExecResult)
    res = await stub(make_exec_input(ECHO_CMD))
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == 0
    assert (res.stdout or "") == "hello"
    assert (res.stderr or "") == ""


@pytest.mark.requires_docker
async def test_exec_roundtrip_echo(mcp_client_box) -> None:
    """Spin up real Docker container and roundtrip an echo via exec without policy gateway."""
    await _assert_exec_echo(mcp_client_box)


@pytest.mark.live_openai_api
@pytest.mark.skipif(os.environ.get("OPENAI_API_KEY") is None, reason="Requires OpenAI API key")
async def test_live_llm_exec_echo(mcp_client_box) -> None:
    """End-to-end: real LLM is instructed to call docker exec to print hello and return exactly it."""
    model_name = os.environ.get("OPENAI_MODEL", "gpt-5")
    client = build_client(model_name)
    agent = await Agent.create(
        mcp_client=mcp_client_box, client=client, handlers=[BaseHandler()], tool_policy=RequireAnyTool()
    )
    agent.process_message(
        SystemMessage.text(
            "You are testing an MCP exec tool.\n"
            "Call the tool "
            f"{build_mcp_function(SERVER_NAME, 'exec')} "
            f"with cmd={ECHO_CMD!r} and return exactly the stdout."
        )
    )
    agent.process_message(UserMessage.text("Run the command now and output exactly the stdout value."))
    res: AgentResult = await agent.run()
    text = (res.text or "").strip()
    assert text == "hello"
