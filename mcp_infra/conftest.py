"""Test fixtures for mcp_infra tests."""

import os
import subprocess
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from rules_python.python.runfiles import runfiles

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core_testing.fixtures import *  # noqa: F403
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.testing.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import make_container_opts

PYTHON_SLIM_IMAGE_TAG = "python-slim:test"


def _get_runfiles_path(relative_path: str) -> Path:
    """Get path to a file in Bazel runfiles."""
    r = runfiles.Create()
    path = r.Rlocation(f"_main/{relative_path}")
    if path:
        return Path(path)

    # Fallback: check bazel-bin for local dev
    repo_root = Path(__file__).parent.parent
    return repo_root / "bazel-bin" / relative_path


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip Docker tests when Docker daemon is not available."""
    if item.get_closest_marker("requires_docker") is None:
        return

    client = None
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


@pytest.fixture(scope="session")
def python_slim_image():
    """Load python-slim image from Bazel :python_slim_load target."""
    load_script = _get_runfiles_path("mcp_infra/testing/python_slim_load.sh")

    result = subprocess.run(
        [load_script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load python-slim image: {result.stderr}")

    return PYTHON_SLIM_IMAGE_TAG


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
