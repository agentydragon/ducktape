"""Driver-hosted MCP tools: a server declared at `initialize` reaches the model as
`mcp__<server>__<tool>`, and a call round-trips entirely over `mcp_message` control requests, with
no real MCP subprocess involved — the tool registry itself is a real `fastmcp.FastMCP` server."""

from __future__ import annotations

import pytest_bazel
from fastmcp import FastMCP

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude.mcp import DriverMcpServer

TOOL_NAME = "mcp__driver__echo"


def _build_server() -> FastMCP:
    server = FastMCP("driver")

    @server.tool
    def echo(message: str) -> str:
        """Echoes the given message back, prefixed."""
        return f"ECHO: {message}"

    return server


DRIVER = DriverMcpServer("driver", _build_server())


async def test_driver_hosted_tool_is_declared_and_called(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages, driver_tools=DRIVER) as run:
        prompt = await run.send("Use the echo tool on 'hi' and report the result.")

        async with await anthropic_messages.await_next_request() as exchange:
            # The MCP initialize/tools-list handshake with our driver-hosted server has already
            # happened during startup, entirely off `mcp_message` control requests; the model's
            # very first request already carries our declared tool under its renamed identity.
            assert TOOL_NAME in exchange.request.tool_names
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", TOOL_NAME, {"message": "hi"})], model=MODEL)
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert result.is_error is False
            assert result.text == "ECHO: hi"
            stream = sse.message_stream([sse.Text("ECHO_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "ECHO_DONE"
    captured = run.native_frames()
    frames.assert_success(captured, "ECHO_DONE")


async def test_unknown_driver_tool_call_reaches_the_model_as_an_error(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """The scenario never registers `fail`, so the model naming it exercises `DriverMcpServer`'s own
    unknown-tool error path rather than actually calling anything."""
    async with claude.start(anthropic_messages, driver_tools=DRIVER) as run:
        prompt = await run.send("Attempt an unregistered driver tool and report what happens.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", "mcp__driver__fail", {})], model=MODEL)
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert result.is_error is True
            stream = sse.message_stream([sse.Text("UNKNOWN_TOOL_OBSERVED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "UNKNOWN_TOOL_OBSERVED"
    captured = run.native_frames()
    frames.assert_success(captured, "UNKNOWN_TOOL_OBSERVED")


if __name__ == "__main__":
    pytest_bazel.main()
