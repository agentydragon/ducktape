"""Pytest fixtures for mcp_infra testing.

Register in downstream packages via:
    pytest_plugins = ["mcp_infra.testing.fixtures"]
"""

from __future__ import annotations

from pathlib import Path

import aiodocker
import pytest

from mcp_infra.exec.docker.types import ContainerExecServerConfig, DefaultValue
from mcp_infra.testing.simple_servers import make_simple_mcp as _make_simple_mcp  # avoid fixture collision


def make_container_opts(image: str, *, working_dir: Path = Path("/test/workspace")) -> ContainerExecServerConfig:
    return ContainerExecServerConfig(
        image=image,
        working_dir=working_dir,
        allow_user_field=True,
        allow_env_field=True,
        cwd_policy=DefaultValue(value=working_dir),
    )


@pytest.fixture
async def async_docker_client():
    client = aiodocker.Docker()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def make_simple_mcp():
    return _make_simple_mcp()
