"""Tests for the in-process `haku_routine` MCP server (build_mcp)."""

from unittest.mock import AsyncMock, Mock

import pytest
import pytest_bazel
from fastmcp import Client

from haku.console.tools.routine import LaunchRoutineResult, build_mcp


@pytest.mark.parametrize(
    ("arguments", "expected_text"),
    [
        pytest.param({"text": "triage open PRs"}, "triage open PRs", id="text"),
        pytest.param({}, None, id="omitted"),
        pytest.param({"text": None}, None, id="explicit_null"),
    ],
)
async def test_launch_routine_dispatches_to_launcher(arguments: dict[str, str | None], expected_text: str | None):
    launcher = Mock()
    launcher.launch = AsyncMock(return_value=LaunchRoutineResult(session_url="https://claude.ai/code/session_x"))
    async with Client(build_mcp(launcher)) as client:
        result = await client.call_tool("launch_routine", arguments)
    assert not result.is_error
    assert result.data.session_url == "https://claude.ai/code/session_x"
    # The tool passes text through verbatim; blank/None normalization lives in launcher.launch.
    launcher.launch.assert_awaited_once_with(expected_text)


if __name__ == "__main__":
    pytest_bazel.main()
