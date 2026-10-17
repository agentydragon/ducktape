"""Pytest fixtures for mcp_infra testing.

Register in downstream packages via:
    pytest_plugins = ["mcp_infra.testing.fixtures"]
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiodocker
import pytest
from fastmcp.client import Client
from fastmcp.mcp_config import StdioMCPServer
from fastmcp.server import FastMCP

from mcp_infra.exec.docker.types import ContainerExecServerConfig, DefaultValue
from mcp_infra.testing.simple_servers import make_simple_mcp as _make_simple_mcp  # avoid fixture collision
from mcp_infra.testing.stubs.typed_stubs import TypedClient
from util.bazel.subprocess import python_env

# The stdio transport fixture launches `-m mcp_infra.testing.stdio_app`; the module
# is referenced only as a string, so gazelle cannot see the dependency.
# gazelle:include_dep //mcp_infra/testing:stdio_app


def make_container_opts(image: str, *, working_dir: Path = Path("/test/workspace")) -> ContainerExecServerConfig:
    """Create standard ContainerExecServerConfig for tests."""
    return ContainerExecServerConfig(
        image=image,
        working_dir=working_dir,
        allow_user_field=True,
        allow_env_field=True,
        cwd_policy=DefaultValue(value=working_dir),
    )


@pytest.fixture
async def async_docker_client():
    """Async Docker client for container tests."""
    client = aiodocker.Docker()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def make_simple_mcp():
    """Lightweight FastMCP backend with simple tools for tests."""
    return _make_simple_mcp()


@pytest.fixture
def make_typed_mcp():
    """Global typed MCP helper yielding (TypedClient, session) for a FastMCP server.

    Usage:
        async with make_typed_mcp(server) as (client, sess):
            ...
    """

    @asynccontextmanager
    async def _open(server: FastMCP):
        async with Client(server) as sess:
            client = TypedClient.from_server(server, sess)
            yield client, sess

    return _open


@pytest.fixture
def stdio_echo_spec() -> StdioMCPServer:
    """Launch packaged echo server module via -m as a stdio spec."""
    return StdioMCPServer(
        command=sys.executable, args=["-m", "mcp_infra.testing.stdio_app"], env=python_env(inherit=False)
    )
