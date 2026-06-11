"""Pytest fixtures for mcp_infra testing.

Register in downstream packages via:
    pytest_plugins = ["mcp_infra.testing.fixtures"]
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiodocker
import pytest
from fastmcp.client import Client
from fastmcp.mcp_config import StdioMCPServer
from fastmcp.server import FastMCP

from mcp_infra.compositor.compositor import Compositor
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.compositor.resources_server import ResourcesServer
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.docker.server import ContainerOptions
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.stubs.resources_stub import ResourcesServerStub
from mcp_infra.stubs.typed_stubs import TypedClient
from mcp_infra.testing.notifications import SubscriptionRecorder, enable_resources_caps, install_subscription_recorder
from mcp_infra.testing.simple_servers import make_simple_mcp as _make_simple_mcp  # avoid fixture collision


def _stdio_env() -> dict[str, str]:
    """Environment for stdio subprocess with Python path forwarded."""
    env: dict[str, str] = {}
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return env


def make_container_opts(image: str, *, working_dir: Path = Path("/workspace")) -> ContainerOptions:
    """Create standard ContainerOptions for tests."""
    return ContainerOptions(image=image, working_dir=working_dir, binds=None)


@pytest.fixture
async def compositor():
    """Fresh Compositor instance for each test with automatic lifecycle management.

    The compositor is entered as a context manager automatically, so tests can
    mount servers and use it immediately without explicit 'async with'.
    """
    async with Compositor() as comp:
        yield comp


@pytest.fixture
async def compositor_client(compositor):
    """Client connected to the compositor."""
    async with Client(compositor) as client:
        yield client


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
def make_typed_mcp():
    """Global typed MCP helper yielding (TypedClient, session) for a FastMCP server.

    Usage:
        async with make_typed_mcp(server) as (client, sess):
            ...
    """

    @asynccontextmanager
    async def _open(server: FastMCP):
        async with Client(server) as sess:
            client = TypedClient.from_server(server, sess)
            yield client, sess

    return _open


@pytest.fixture
async def resources_server(compositor):
    """Resources server for the compositor."""
    return ResourcesServer(compositor=compositor)


@pytest.fixture
async def resources_client(resources_server):
    """Client for the resources server."""
    async with Client(resources_server) as client:
        yield client


@pytest.fixture
async def typed_resources_client(resources_server, resources_client):
    """Typed stub for the resources server."""
    return ResourcesServerStub.from_server(resources_server, resources_client)


@pytest.fixture
def stdio_echo_spec() -> StdioMCPServer:
    """Launch packaged echo server module via -m as a stdio spec."""
    return StdioMCPServer(command=sys.executable, args=["-m", "mcp_infra.testing.stdio_app"], env=_stdio_env())


@pytest.fixture
def stdio_notifier_spec() -> StdioMCPServer:
    """Launch notification-emitting server via -m as stdio spec."""
    return StdioMCPServer(command=sys.executable, args=["-m", "mcp_infra.testing.stdio_notifier"], env=_stdio_env())


@pytest.fixture
def origin_with_recorder() -> tuple[FastMCP, SubscriptionRecorder]:
    """Origin server with subscription recorder attached."""
    # Workaround: Pass version="test" to skip slow importlib.metadata.version() lookup
    # that hangs on os.stat() in Nix environment. Without this, MCP server initialization
    # would call pkg_version("mcp") which triggers filesystem operations that timeout.
    m = EnhancedFastMCP("origin", version="test")
    recorder = install_subscription_recorder(m)

    @m.resource("resource://foo/bar", name="dummy", mime_type="text/plain", description="dummy")
    async def foo_bar() -> str:
        return "ok"

    # Ensure this origin advertises resources.subscribe for gating and
    # registers explicit handlers so subscribe/unsubscribe calls succeed.
    enable_resources_caps(m, subscribe=True)
    return m, recorder


@pytest.fixture
def make_compositor():
    """Async helper to open a Compositor and yield (Client, Compositor).

    Usage:
        async with make_compositor({"name": server, ...}) as (client, comp):
            ...
    """

    @asynccontextmanager
    async def _open(servers: dict[str, FastMCP]):
        async with Compositor() as comp:
            for name, srv in servers.items():
                await comp.mount_inproc(MCPMountPrefix(name), srv)
            async with Client(comp) as sess:
                yield sess, comp

    return _open


@pytest.fixture
def make_buffered_client():
    """Async helper to open a Compositor + Client with NotificationsBuffer.

    Usage:
        async with make_buffered_client({"name": server, ...}) as (client, comp, buf):
            ...
    """

    @asynccontextmanager
    async def _open(servers: dict[str, FastMCP]):
        async with Compositor(version="1.0.0-test") as comp:
            for name, srv in servers.items():
                await comp.mount_inproc(MCPMountPrefix(name), srv)
            buf = NotificationsBuffer(compositor=comp)
            async with Client(comp, message_handler=buf.handler) as sess:
                yield sess, comp, buf

    return _open
