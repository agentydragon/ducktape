from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from fastmcp.client import Client

from mcp_infra.constants import WORKING_DIR
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, ContainerExecServerConfig, DefaultValue, ModelChooses

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
        assert "cwd" not in exec_tool.input_schema["properties"]

        result = await c.call_tool("exec", {"cmd": ["pwd"], "timeout_ms": 10000})
        text = result.content[0].text if result.content else ""
        assert "/tmp" in text


async def test_cwd_model_chooses(make_cwd_server) -> None:
    """ModelChooses: cwd is required in the schema, model must provide it."""
    server = make_cwd_server(ModelChooses())
    async with Client(server) as c:
        tools = await c.list_tools()
        exec_tool = next(t for t in tools if t.name == "exec")
        assert "cwd" in exec_tool.input_schema["properties"]
        assert "cwd" in exec_tool.input_schema["required"]

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
