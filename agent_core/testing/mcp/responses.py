"""Echo tool response mock for MCP agent tests."""

from __future__ import annotations

from collections.abc import Generator

from agent_core.testing.mcp.echo_server import ECHO_TOOL_NAME, EchoInput, EchoOutput
from agent_core.testing.responses import DecoratorMock, tool_roundtrip
from openai_utils.model import FunctionCallItem, ResponsesRequest


class EchoMock(DecoratorMock):
    """Mock with echo server helpers.

    Example:
        @EchoMock.mock()
        def mock(m: EchoMock):
            req = yield
            result = yield from m.echo_roundtrip("hello")
            assert result.echo == "hello"
    """

    def echo_call(self, text: str) -> FunctionCallItem:
        """Create echo tool call item."""
        return self.tool_call(ECHO_TOOL_NAME, EchoInput(text=text))

    def echo_roundtrip(self, text: str) -> Generator[FunctionCallItem, ResponsesRequest, EchoOutput]:
        """Yield echo call, receive response, return typed result."""
        return tool_roundtrip(self.echo_call(text), EchoOutput)
