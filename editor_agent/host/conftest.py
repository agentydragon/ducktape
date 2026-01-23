from __future__ import annotations

from contextlib import suppress

import docker
import pytest

from agent_pkg.host.builder import ensure_image
from editor_agent.host.cli import _DOCKERFILE, _REPO_ROOT

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403

EDITOR_IMAGE_TAG = "adgn-editor:test"


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


@pytest.fixture
async def editor_image_id(async_docker_client):
    """Build or retrieve editor agent image."""
    return await ensure_image(async_docker_client, _REPO_ROOT, EDITOR_IMAGE_TAG, dockerfile=_DOCKERFILE)
