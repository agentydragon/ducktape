from __future__ import annotations

import os

import pytest
from hamcrest import all_of, assert_that, has_properties, instance_of

from adgn.agent.agent import AgentResult, MiniCodex
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.exec.models import BaseExecResult, ExecInput, Exited
from adgn.mcp.stubs.typed_stubs import ToolStub
from adgn.openai_utils.client_factory import build_client

# Use /bin/echo -n for portability and to avoid trailing newline
ECHO_CMD = ["/bin/echo", "-n", "hello"]

SERVER_NAME = "box"


async def _assert_exec_echo(sess) -> None:
    # Call via compositor using namespaced tool key
    stub = ToolStub(sess, build_mcp_function(SERVER_NAME, "exec"), BaseExecResult)
    assert_that(
        await stub(ExecInput(cmd=ECHO_CMD, timeout_ms=10_000)),
        has_properties(
            exit=all_of(instance_of(Exited), has_properties(exit_code=0)),
            stdout="hello",
            stderr="",
        ),
    )


@pytest.mark.requires_docker
async def test_exec_roundtrip_echo(make_pg_compositor_box) -> None:
    """Spin up real Docker container and roundtrip an echo via exec."""
    async with make_pg_compositor_box() as (mcp_client, _comp):
        await _assert_exec_echo(mcp_client)


@pytest.mark.live_llm
@pytest.mark.skipif(os.environ.get("OPENAI_API_KEY") is None, reason="Requires OpenAI API key")
async def test_live_llm_exec_echo(make_pg_compositor_box) -> None:
    """End-to-end: real LLM is instructed to call docker exec to print hello and return exactly it."""

    async with make_pg_compositor_box() as (mcp_client, _comp):
        model_name = os.environ.get("OPENAI_MODEL", "gpt-5")
        client = build_client(model_name)
        agent = await MiniCodex.create(
            model=model_name,
            mcp_client=mcp_client,
            system=(
                "You are testing an MCP exec tool.\n"
                "Call the tool "
                f"{build_mcp_function(SERVER_NAME, 'exec')} "
                f"with cmd={ECHO_CMD!r} and return exactly the stdout."
            ),
            client=client,
            handlers=[AutoHandler()],
        )
        res: AgentResult = await agent.run("Run the command now and output exactly the stdout value.")
        text = (res.text or "").strip()
        assert text == "hello"
