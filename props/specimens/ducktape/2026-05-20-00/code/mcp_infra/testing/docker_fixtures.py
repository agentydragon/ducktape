"""Docker-related pytest fixtures for mcp_infra tests.

These fixtures depend on the debian-slim OCI image loaded from Bazel.
Import explicitly in tests that need Docker execution.
"""

from __future__ import annotations

import pytest

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.testing.fixtures import make_container_opts


@pytest.fixture
async def docker_exec_server(async_docker_client, debian_slim_image):
    """Canonical Docker exec server using debian-slim image."""
    opts = make_container_opts(debian_slim_image)
    return ContainerExecServer(async_docker_client, opts)
