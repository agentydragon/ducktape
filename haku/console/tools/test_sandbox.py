"""FastMCP contract tests for the autonomous haku_sandbox tool surface."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest_bazel
from fastmcp import Client

from haku.console.tools.sandbox import HAKU_SANDBOX_SERVER_ID, SandboxExecResult, SandboxInfo, build_mcp
from mcp_infra.exec.models import Exited


async def test_tool_surface() -> None:
    async with Client(build_mcp(Mock())) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"reserve", "exec", "info"}
    assert HAKU_SANDBOX_SERVER_ID == "haku_sandbox"


async def test_tools_forward_and_return_structured_state() -> None:
    expires_at = datetime(2026, 7, 23, tzinfo=UTC)
    backend = Mock()
    backend.reserve = AsyncMock(return_value="hs-k7q2m")
    backend.execute = AsyncMock(
        return_value=SandboxExecResult(
            exit=Exited(exit_code=0), stdout="ok\n", stderr="", duration_ms=4, expires_at=expires_at
        )
    )
    backend.info = AsyncMock(
        return_value=SandboxInfo(
            handle="hs-k7q2m", state="ready", healthy=True, expires_at=expires_at, pod_name="haku-bash-abcde"
        )
    )

    async with Client(build_mcp(backend)) as client:
        reserved = await client.call_tool("reserve", {})
        executed = await client.call_tool(
            "exec", {"handle": "hs-k7q2m", "cmd": ["printf", "ok\\n"], "timeout_ms": 5000}
        )
        inspected = await client.call_tool("info", {"handle": "hs-k7q2m"})

    assert reserved.data == "hs-k7q2m"
    assert executed.data.stdout == "ok\n"
    assert executed.data.exit == {"kind": "exited", "exit_code": 0}
    assert inspected.data.state == "ready"
    backend.execute.assert_awaited_once_with(
        handle="hs-k7q2m", cmd=["printf", "ok\\n"], max_bytes=100_000, timeout_ms=5000
    )


async def test_exec_schema_enforces_handle_and_five_minute_cap() -> None:
    backend = Mock()
    backend.execute = AsyncMock()
    async with Client(build_mcp(backend)) as client:
        bad_handle = await client.call_tool(
            "exec", {"handle": "not-a-handle", "cmd": ["true"], "timeout_ms": 1000}, raise_on_error=False
        )
        too_long = await client.call_tool(
            "exec", {"handle": "hs-abc12", "cmd": ["true"], "timeout_ms": 300_001}, raise_on_error=False
        )
    assert bad_handle.is_error
    assert too_long.is_error
    backend.execute.assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()
