"""Tests for the in-process `haku_routine` MCP server (build_mcp)."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp import Client
from pydantic import SecretStr

from haku.console.config import LaunchRoutineConfig
from haku.console.tools.routine import LaunchRoutineResult, RoutineLauncher, build_mcp


def _mcp(launcher=None):
    return build_mcp(launcher or Mock())


async def test_tool_surface():
    async with Client(_mcp()) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"launch_routine"}


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


async def test_launch_routine_accepts_explicit_null_text():
    launcher = Mock()
    launcher.launch = AsyncMock(return_value=LaunchRoutineResult(session_url="https://x/s"))
    async with Client(_mcp(launcher=launcher)) as client:
        result = await client.call_tool("launch_routine", {"text": None})
    assert not result.is_error
    launcher.launch.assert_awaited_once_with(None)


ROUTINE_ID = "trig_test"
FIRE_URL = f"https://api.anthropic.com/v1/claude_code/routines/{ROUTINE_ID}/fire"


def _launcher() -> RoutineLauncher:
    return RoutineLauncher(LaunchRoutineConfig(routine_id=ROUTINE_ID, token=SecretStr("sk-test-token")))


async def test_launch_fires_with_server_side_bearer_and_no_text() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(
            return_value=httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_x"})
        )
        result = await _launcher().launch(None)
    assert result.session_url == "https://claude.ai/code/session_x"
    sent = route.calls.last.request
    # The bearer + required anthropic-version header are attached server-side.
    assert sent.headers["authorization"] == "Bearer sk-test-token"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(sent.content) == {}


async def test_launch_forwards_custom_text() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200, json={"claude_code_session_url": "u"}))
        await _launcher().launch("scan CPAP and summarize anomalies")
    assert json.loads(route.calls.last.request.content) == {"text": "scan CPAP and summarize anomalies"}


async def test_launch_blank_text_uses_routine_default() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200, json={"claude_code_session_url": "u"}))
        await _launcher().launch("   ")
    # Blank/whitespace collapses to the routine's saved default (no text field sent).
    assert json.loads(route.calls.last.request.content) == {}


async def test_launch_raises_with_upstream_detail_on_error() -> None:
    with respx.mock:
        respx.post(FIRE_URL).mock(
            return_value=httpx.Response(400, json={"error": {"message": "anthropic-version: header is required"}})
        )
        with pytest.raises(RuntimeError, match="anthropic-version: header is required"):
            await _launcher().launch(None)


if __name__ == "__main__":
    pytest_bazel.main()
