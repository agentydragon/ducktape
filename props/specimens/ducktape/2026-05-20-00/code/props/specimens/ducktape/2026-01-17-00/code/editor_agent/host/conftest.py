from __future__ import annotations

import pytest

from agent_pkg.host.builder import ensure_image
from editor_agent.host.cli import _DOCKERFILE, _REPO_ROOT

pytest_plugins = ["agent_core_testing.docker"]

EDITOR_IMAGE_TAG = "adgn-editor:test"


@pytest.fixture
async def editor_image_id(async_docker_client):
    """Build or retrieve editor agent image."""
    return await ensure_image(async_docker_client, _REPO_ROOT, EDITOR_IMAGE_TAG, dockerfile=_DOCKERFILE)
