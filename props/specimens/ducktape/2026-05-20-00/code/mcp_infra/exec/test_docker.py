from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from fastmcp.client import Client

from mcp_infra.constants import WORKING_DIR
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, ContainerExecServerConfig, DefaultValue, ModelChooses
from mcp_infra.exec.models import BaseExecResult, Exited, TimedOut


@pytest.fixture
async def docker_client_session(docker_exec_server):
    """FastMCP Client session for docker exec server with debian-slim."""
    async with Client(docker_exec_server) as session:
        yield session


async def _call_exec(session: Client, cmd: list[str], *, timeout_ms: int = 10000) -> BaseExecResult:
    """Call exec tool and parse structured result.

    Includes all fields required by the default fixture (allow_env=True, allow_user=True).
    """
    args = {"cmd": cmd, "timeout_ms": timeout_ms, "env": None, "user": None, "cwd": None}
    result = await session.call_tool("exec", args)
    return BaseExecResult.model_validate(result.structured_content)


async def test_hello_world(docker_client_session) -> None:
    tools = await docker_client_session.list_tools()
    names = {t.name for t in tools}
    assert "exec" in names

    res = await _call_exec(docker_client_session, ["/bin/echo", "hello"])
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == 0
    assert isinstance(res.stdout, str)
    assert "hello" in (res.stdout or "")


async def test_stderr_and_exit_code(docker_client_session) -> None:
    res = await _call_exec(docker_client_session, ["sh", "-lc", "echo err 1>&2; exit 3"])
    expected_err_exit = 3
    assert isinstance(res.exit, Exited)
    assert res.exit.exit_code == expected_err_exit
    assert isinstance(res.stderr, str)
    assert "err" in (res.stderr or "")


async def test_timeout_flag(docker_client_session) -> None:
    res = await _call_exec(docker_client_session, ["sh", "-lc", "sleep 5"], timeout_ms=500)
    assert isinstance(res.exit, TimedOut)


# -- CwdPolicy tests: verify each mode runs commands in the expected directory --


@pytest.fixture
def make_cwd_server(async_docker_client, debian_slim_image):
    """Factory for ContainerExecServer with a specific cwd_policy."""

    def _factory(cwd_policy):
        config = ContainerExecServerConfig(
            image=debian_slim_image,
            working_dir=WORKING_DIR,
            allow_user_field=False,
            allow_env_field=False,
            cwd_policy=cwd_policy,
        )
        return ContainerExecServer(async_docker_client, config)

    return _factory


async def test_cwd_default_value(make_cwd_server) -> None:
    """DefaultValue: command runs in the default cwd when cwd is omitted."""
    server = make_cwd_server(DefaultValue(value=WORKING_DIR))
    async with Client(server) as c:
        result = await c.call_tool("exec", {"cmd": ["pwd"], "timeout_ms": 10000})
        text = result.content[0].text if result.content else ""
        assert str(WORKING_DIR) in text


async def test_cwd_default_value_override(make_cwd_server) -> None:
    """DefaultValue: model can override cwd."""
    server = make_cwd_server(DefaultValue(value=WORKING_DIR))
    async with Client(server) as c:
        result = await c.call_tool("exec", {"cmd": ["pwd"], "cwd": "/tmp", "timeout_ms": 10000})
        text = result.content[0].text if result.content else ""
        assert "/tmp" in text


async def test_cwd_always_set_to(make_cwd_server) -> None:
    """AlwaysSetTo: command always runs in the fixed cwd, field hidden from schema."""
    server = make_cwd_server(AlwaysSetTo(value=Path("/tmp")))
    async with Client(server) as c:
        tools = await c.list_tools()
        exec_tool = next(t for t in tools if t.name == "exec")
        assert "cwd" not in exec_tool.inputSchema["properties"]

        result = await c.call_tool("exec", {"cmd": ["pwd"], "timeout_ms": 10000})
        text = result.content[0].text if result.content else ""
        assert "/tmp" in text


async def test_cwd_model_chooses(make_cwd_server) -> None:
    """ModelChooses: cwd is required in the schema, model must provide it."""
    server = make_cwd_server(ModelChooses())
    async with Client(server) as c:
        tools = await c.list_tools()
        exec_tool = next(t for t in tools if t.name == "exec")
        assert "cwd" in exec_tool.inputSchema["properties"]
        assert "cwd" in exec_tool.inputSchema["required"]

        result = await c.call_tool("exec", {"cmd": ["pwd"], "cwd": "/tmp", "timeout_ms": 10000})
        text = result.content[0].text if result.content else ""
        assert "/tmp" in text


async def test_cwd_always_set_to_description(make_cwd_server) -> None:
    """AlwaysSetTo: tool description mentions the fixed cwd."""
    server = make_cwd_server(AlwaysSetTo(value=Path("/var")))
    async with Client(server) as c:
        tools = await c.list_tools()
        exec_tool = next(t for t in tools if t.name == "exec")
        assert "/var" in (exec_tool.description or "")


if __name__ == "__main__":
    pytest_bazel.main()
