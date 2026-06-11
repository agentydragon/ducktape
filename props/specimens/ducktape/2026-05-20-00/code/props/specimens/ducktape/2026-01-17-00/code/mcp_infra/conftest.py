"""Test fixtures for mcp_infra tests."""

from contextlib import suppress

import docker
import pytest

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.testing.fixtures import make_container_opts

# Register mcp_infra and agent_core fixtures
pytest_plugins = ["mcp_infra.testing.fixtures", "agent_core_testing.fixtures"]


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip Docker tests when Docker daemon is not available or images are missing."""
    if item.get_closest_marker("requires_docker") is None:
        return

    client = None
    try:
        client = docker.from_env()
        client.ping()

        # Check if required images are available
        try:
            client.images.get("python:3.12-slim")
        except docker.errors.ImageNotFound:
            pytest.skip("Docker image python:3.12-slim not available (run: docker pull python:3.12-slim)")
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


@pytest.fixture
async def docker_exec_server_py312slim(async_docker_client):
    """Canonical Docker exec server using python:3.12-slim image."""
    opts = make_container_opts("python:3.12-slim")
    return ContainerExecServer(async_docker_client, opts)


@pytest.fixture
async def typed_docker_client(make_typed_mcp, docker_exec_server_py312slim):
    """Typed MCP client for docker exec server with python:3.12-slim.

    Yields (TypedClient, session) tuple for direct use in tests.
    """
    async with make_typed_mcp(docker_exec_server_py312slim) as (client, session):
        yield client, session
