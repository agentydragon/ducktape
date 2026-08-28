"""Tests for the in-process `hostexec` MCP server (build_mcp): tool surface, cmd forwarding, and
that the schema rejects an invalid run_as before the tool body runs."""

from unittest.mock import AsyncMock, Mock

import pytest_bazel
from fastmcp import Client

from haku.console.tools.hostexec import build_mcp
from mcp_infra.exec.models import BaseExecResult, Exited


def _result() -> BaseExecResult:
    return BaseExecResult(exit=Exited(exit_code=0), stdout="ok", stderr="", duration_ms=3)


async def test_tool_surface() -> None:
    async with Client(build_mcp(Mock())) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"bash"}


async def test_bash_forwards_cmd_and_returns_result() -> None:
    client_mock = Mock()
    client_mock.run = AsyncMock(return_value=_result())
    async with Client(build_mcp(client_mock)) as client:
        result = await client.call_tool(
            "bash", {"host": "wyrm2", "run_as": "root", "cmd": "ls -la", "max_bytes": 2000, "timeout_ms": 5000}
        )
    assert not result.is_error
    # FastMCP returns the discriminated-union `exit` field as a plain dict in result.data.
    assert result.data.exit == {"kind": "exited", "exit_code": 0}
    assert result.data.stdout == "ok"
    client_mock.run.assert_awaited_once_with(
        host="wyrm2", run_as="root", cmd="ls -la", cwd=None, max_bytes=2000, timeout_ms=5000
    )


async def test_bash_rejects_invalid_run_as() -> None:
    # RunAsUser's pattern forbids spaces/uppercase; the input schema rejects it before the tool runs.
    client_mock = Mock()
    client_mock.run = AsyncMock(return_value=_result())
    async with Client(build_mcp(client_mock)) as client:
        result = await client.call_tool(
            "bash",
            {"host": "wyrm2", "run_as": "Root User", "cmd": "true", "max_bytes": 0, "timeout_ms": 1000},
            raise_on_error=False,
        )
    assert result.is_error
    client_mock.run.assert_not_awaited()


async def test_bash_rejects_empty_cmd() -> None:
    # cmd's min_length=1 rejects an empty script before the tool runs.
    client_mock = Mock()
    client_mock.run = AsyncMock(return_value=_result())
    async with Client(build_mcp(client_mock)) as client:
        result = await client.call_tool(
            "bash",
            {"host": "wyrm2", "run_as": "root", "cmd": "", "max_bytes": 0, "timeout_ms": 1000},
            raise_on_error=False,
        )
    assert result.is_error
    client_mock.run.assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()
