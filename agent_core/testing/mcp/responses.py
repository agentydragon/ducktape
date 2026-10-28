"""MCP-dependent response factories and mock classes for agent tests.

Extends the non-MCP base classes from agent_core.testing.responses with
MCP-aware convenience methods.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

import pytest
from pydantic import BaseModel

from agent_core.testing.mcp.echo_server import ECHO_TOOL_NAME, EchoInput, EchoOutput
from agent_core.testing.responses import DecoratorMock, ResponsesFactory, tool_roundtrip
from mcp_infra.testing.naming import build_mcp_function
from openai_utils.model import FunctionCallItem, ResponsesRequest


class MCPResponsesFactory(ResponsesFactory):
    """ResponsesFactory with MCP-aware convenience methods."""

    def mcp_tool_call(
        self, server: str, tool: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(build_mcp_function(server, tool), args, call_id)


class MCPDecoratorMock(DecoratorMock):
    """DecoratorMock with MCP-aware convenience methods.

    Use this base class instead of DecoratorMock when your mock needs
    call_roundtrip or mcp_tool_call.
    """

    def call_roundtrip[U: BaseModel](
        self, prefix: str, tool: str | Callable[..., Any], arguments: dict[str, Any] | BaseModel, output_type: type[U]
    ) -> Generator[FunctionCallItem, ResponsesRequest, U]:
        """Create tool call and yield roundtrip generator."""
        tool_name = tool if isinstance(tool, str) else tool.__name__
        call = self.mcp_tool_call(prefix, tool_name, arguments)
        return tool_roundtrip(call, output_type)

    def mcp_tool_call(
        self, server: str, tool: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(build_mcp_function(server, tool), args, call_id)


class EchoMock(MCPDecoratorMock):
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


# ---- Pytest fixtures ----


@pytest.fixture(scope="session")
def responses_factory(reasoning_model: str) -> MCPResponsesFactory:
    """MCP-aware responses factory — the only responses_factory definition."""
    return MCPResponsesFactory(reasoning_model)
