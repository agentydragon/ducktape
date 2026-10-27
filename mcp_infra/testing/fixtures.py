"""Pytest fixtures for mcp_infra testing.

Register in downstream packages via:
    pytest_plugins = ["mcp_infra.testing.fixtures"]
"""

from __future__ import annotations

import sys
from pathlib import Path

import aiodocker
import pytest
from fastmcp.mcp_config import StdioMCPServer

from mcp_infra.exec.docker.types import ContainerExecServerConfig, DefaultValue
from mcp_infra.testing.simple_servers import make_simple_mcp as _make_simple_mcp  # avoid fixture collision
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
def stdio_echo_spec() -> StdioMCPServer:
    """Launch packaged echo server module via -m as a stdio spec."""
    return StdioMCPServer(
        command=sys.executable, args=["-m", "mcp_infra.testing.stdio_app"], env=python_env(inherit=False)
    )
