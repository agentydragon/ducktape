from __future__ import annotations

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403
from agent_pkg.host.builder import ensure_image
from editor_agent.host.cli import _DOCKERFILE, _REPO_ROOT

EDITOR_IMAGE_TAG = "adgn-editor:test"


@pytest.fixture
async def editor_image_id(async_docker_client):
    """Build or retrieve editor agent image."""
    return await ensure_image(async_docker_client, _REPO_ROOT, EDITOR_IMAGE_TAG, dockerfile=_DOCKERFILE)
