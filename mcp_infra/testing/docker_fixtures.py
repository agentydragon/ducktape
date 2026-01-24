"""Docker-related pytest fixtures for mcp_infra tests.

These fixtures depend on the python-slim OCI image loaded from Bazel.
Import explicitly in tests that need Docker execution.
"""

from __future__ import annotations

import pytest

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.testing.fixtures import make_container_opts


@pytest.fixture
async def docker_exec_server_py312slim(async_docker_client, python_slim_image):
    """Canonical Docker exec server using python-slim image."""
    opts = make_container_opts(python_slim_image)
    return ContainerExecServer(async_docker_client, opts)


@pytest.fixture
async def typed_docker_client(make_typed_mcp, docker_exec_server_py312slim):
    """Typed MCP client for docker exec server with python:3.12-slim.

    Yields (TypedClient, session) tuple for direct use in tests.
    """
    async with make_typed_mcp(docker_exec_server_py312slim) as (client, session):
        yield client, session
