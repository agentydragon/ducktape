from __future__ import annotations

import pytest

from mcp_infra.exec.models import Exited, TimedOut, make_exec_input

# All tests below require structuredContent and call via the typed client


@pytest.mark.requires_docker
async def test_hello_world(typed_docker_client) -> None:
    client, session = typed_docker_client
    tools = await session.list_tools()
    names = {t.name for t in tools}
    assert "exec" in names

    res = await client.exec(make_exec_input(["/bin/echo", "hello"]))
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == 0
    assert isinstance(res.stdout, str)  # Short output should not be truncated
    assert "hello" in (res.stdout or "")


@pytest.mark.requires_docker
async def test_stderr_and_exit_code(typed_docker_client) -> None:
    client, _session = typed_docker_client
    res = await client.exec(make_exec_input(["sh", "-lc", "echo err 1>&2; exit 3"]))
    expected_err_exit = 3
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == expected_err_exit
    assert isinstance(res.stderr, str)  # Short error should not be truncated
    assert "err" in (res.stderr or "")


@pytest.mark.requires_docker
async def test_timeout_flag(typed_docker_client) -> None:
    client, _session = typed_docker_client
    res = await client.exec(make_exec_input(["sh", "-lc", "sleep 5"], timeout_ms=500))
    assert isinstance(res.exit, TimedOut)
