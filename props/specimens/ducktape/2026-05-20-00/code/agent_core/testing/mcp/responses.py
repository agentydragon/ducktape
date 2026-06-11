"""MCP-dependent response factories and mock classes for agent tests.

Extends the non-MCP base classes from agent_core.testing.responses with
MCP-aware convenience methods.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.tools.tool import Tool
from pydantic import BaseModel

from agent_core.testing.mcp.echo_server import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput, EchoOutput
from agent_core.testing.responses import DecoratorMock, PlayGen, ResponsesFactory, tool_roundtrip
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.mounted import Mounted
from mcp_infra.naming import MCPMountPrefix, build_mcp_function
from openai_utils.model import FunctionCallItem, ResponsesRequest


class MCPResponsesFactory(ResponsesFactory):
    """ResponsesFactory with MCP-aware convenience methods."""

    def mcp_tool_call(
        self, server: MCPMountPrefix, tool: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(build_mcp_function(server, tool), args, call_id)

    def docker_exec(
        self,
        cmd: list[str],
        *,
        timeout_ms: int = 30000,
        cwd: str | None = None,
        env: list[str] | None = None,
        user: str | None = None,
        tool_name: str = "exec",
    ) -> FunctionCallItem:
        """Create docker exec tool call with sensible defaults."""
        args: dict[str, Any] = {"cmd": cmd, "timeout_ms": timeout_ms}
        if cwd is not None:
            args["cwd"] = cwd
        if env is not None:
            args["env"] = env
        if user is not None:
            args["user"] = user
        return self.mcp_tool_call(ContainerExecServer.DOCKER_MOUNT_PREFIX, tool_name, args)

    def mounted_tool_call[S: FastMCP](
        self, mounted: Mounted[S], tool: Tool, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call from Mounted server + tool attribute.

        Preferred over mcp_tool_call when you have a Mounted wrapper, as it
        derives the fully-qualified tool name from the Tool attribute.
        """
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(mounted.tool_name(tool), args, call_id)


class MCPDecoratorMock(DecoratorMock):
    """DecoratorMock with MCP-aware convenience methods.

    Use this base class instead of DecoratorMock when your mock needs
    call_roundtrip or mcp_tool_call.
    """

    def call_roundtrip[S: FastMCP, U: BaseModel](
        self, mounted: Mounted[S], tool: Tool, arguments: dict[str, Any] | BaseModel, output_type: type[U]
    ) -> Generator[FunctionCallItem, ResponsesRequest, U]:
        """Create tool call and yield roundtrip generator."""
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        call = self.tool_call(mounted.tool_name(tool), args)
        return tool_roundtrip(call, output_type)

    def mcp_tool_call(
        self, server: MCPMountPrefix, tool: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        args = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(build_mcp_function(server, tool), args, call_id)


class DockerExecMock(MCPDecoratorMock):
    """Mock with docker exec helpers.

    Example:
        @DockerExecMock.mock(runtime)
        def mock(m: DockerExecMock):
            req = yield
            result = yield from m.exec(["ls", "-la"])
            assert result.exit.exit_code == 0
            yield from m.exec(["submit"])
    """

    def __init__(
        self,
        play_fn: Callable[[DockerExecMock], PlayGen],
        runtime: Mounted[ContainerExecServer],
        *,
        check_consumed: bool = True,
    ) -> None:
        self._runtime = runtime
        # Safe: play() only calls play_fn with self which is DockerExecMock
        super().__init__(play_fn, check_consumed=check_consumed)  # type: ignore[arg-type]

    def exec(
        self, cmd: list[str], timeout_ms: int = 30000, cwd: str | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield exec call, receive response, return typed result."""
        args: dict[str, Any] = {"cmd": cmd, "timeout_ms": timeout_ms}
        if cwd is not None:
            args["cwd"] = cwd
        return self.call_roundtrip(self._runtime, self._runtime.server.exec_tool, args, BaseExecResult)


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
        return self.tool_call(build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME), EchoInput(text=text))

    def echo_roundtrip(self, text: str) -> Generator[FunctionCallItem, ResponsesRequest, EchoOutput]:
        """Yield echo call, receive response, return typed result."""
        return tool_roundtrip(self.echo_call(text), EchoOutput)


# ---- Pytest fixtures ----


@pytest.fixture(scope="session")
def responses_factory(reasoning_model: str) -> MCPResponsesFactory:
    """MCP-aware responses factory — the only responses_factory definition."""
    return MCPResponsesFactory(reasoning_model)
