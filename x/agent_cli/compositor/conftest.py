"""Fixtures for compositor tests."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastmcp.client import Client
from fastmcp.server import FastMCP

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.responses import reasoning_model
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.docker_fixtures import docker_exec_server
from mcp_infra.testing.fixtures import *  # noqa: F403
from third_party.containers.fixtures import debian_slim_image
from x.agent_cli.compositor.compositor import Compositor
from x.agent_cli.compositor.resources_server import ResourcesServer
from x.agent_cli.compositor.resources_stub import ResourcesServerStub


@pytest.fixture
async def compositor():
    """Fresh agent CLI compositor with automatic lifecycle management."""
    async with Compositor() as comp:
        yield comp


@pytest.fixture
async def compositor_client(compositor):
    """Client connected to the compositor."""
    async with Client(compositor) as client:
        yield client


@pytest.fixture
async def resources_server(compositor):
    """Resources server for the compositor."""
    return ResourcesServer(compositor=compositor)


@pytest.fixture
async def resources_client(resources_server):
    async with Client(resources_server) as client:
        yield client


@pytest.fixture
async def typed_resources_client(resources_server, resources_client):
    return ResourcesServerStub.from_server(resources_server, resources_client)


@pytest.fixture
def make_compositor():
    """Open a compositor with the supplied in-process servers mounted."""

    @asynccontextmanager
    async def _open(servers: dict[str, FastMCP]):
        async with Compositor() as comp:
            for name, srv in servers.items():
                await comp.mount_inproc(MCPMountPrefix(name), srv)
            async with Client(comp) as sess:
                yield sess, comp

    return _open


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
