"""Tests for the in-process `haku_routine` MCP server (build_mcp)."""

from unittest.mock import AsyncMock, Mock

import pytest_bazel
from fastmcp import Client

from haku.console.tools.routine import HAKU_ROUTINE_SERVER_ID, LaunchRoutineResult, build_mcp


def _mcp(launcher=None):
    return build_mcp(launcher or Mock())


async def test_tool_surface():
    async with Client(_mcp()) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"launch_routine"}
    assert HAKU_ROUTINE_SERVER_ID == "haku_routine"


async def test_launch_routine_dispatches_to_launcher():
    launcher = Mock()
    launcher.launch = AsyncMock(return_value=LaunchRoutineResult(session_url="https://claude.ai/code/session_x"))
    async with Client(_mcp(launcher=launcher)) as client:
        result = await client.call_tool("launch_routine", {"text": "triage open PRs"})
    assert not result.is_error
    assert result.data.session_url == "https://claude.ai/code/session_x"
    # The tool passes text through verbatim; blank/None normalization lives in launcher.launch.
    launcher.launch.assert_awaited_once_with("triage open PRs")


async def test_launch_routine_defaults_text_to_none():
    launcher = Mock()
    launcher.launch = AsyncMock(return_value=LaunchRoutineResult(session_url="https://x/s"))
    async with Client(_mcp(launcher=launcher)) as client:
        await client.call_tool("launch_routine", {})
    launcher.launch.assert_awaited_once_with(None)


if __name__ == "__main__":
    pytest_bazel.main()
