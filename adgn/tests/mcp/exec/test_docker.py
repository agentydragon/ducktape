from __future__ import annotations

import pytest

from adgn.mcp.exec.models import ExecInput, Exited, TimedOut

# All tests below require structuredContent and call via the typed client


@pytest.mark.requires_docker
async def test_hello_world(docker_exec_server_alpine, make_typed_mcp) -> None:
    async with make_typed_mcp(docker_exec_server_alpine, "docker") as (client, session):
        tools = await session.list_tools()
        names = {t.name for t in tools}
        assert "docker_exec" in names

        res = await client.docker_exec(ExecInput(cmd=["/bin/echo", "hello"], timeout_ms=10_000))
        assert isinstance(res.exit, Exited)
        assert res.exit.exit_code == 0
        assert isinstance(res.stdout, str)  # Short output should not be truncated
        assert "hello" in (res.stdout or "")


@pytest.mark.requires_docker
async def test_stderr_and_exit_code(docker_exec_server_alpine, make_typed_mcp) -> None:
    async with make_typed_mcp(docker_exec_server_alpine, "docker") as (client, _session):
        res = await client.docker_exec(ExecInput(cmd=["sh", "-lc", "echo err 1>&2; exit 3"], timeout_ms=10_000))
        expected_err_exit = 3
        assert isinstance(res.exit, Exited)
        assert res.exit.exit_code == expected_err_exit
        assert isinstance(res.stderr, str)  # Short error should not be truncated
        assert "err" in (res.stderr or "")


@pytest.mark.requires_docker
async def test_timeout_flag(docker_exec_server_alpine, make_typed_mcp) -> None:
    async with make_typed_mcp(docker_exec_server_alpine, "docker") as (client, _session):
        res = await client.docker_exec(ExecInput(cmd=["sh", "-lc", "sleep 5"], timeout_ms=500))
        assert isinstance(res.exit, TimedOut)
