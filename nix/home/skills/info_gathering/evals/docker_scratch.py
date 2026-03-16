"""Docker scratch space for eval agents.

Uses the existing ContainerExecServer (from mcp_infra) in-process via FastMCP Client.
The server registers an `exec` tool and manages container lifecycle via its lifespan.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiodocker
from fastmcp.client import Client

from agent_core.mcp_provider import MCPToolProvider
from mcp_infra.exec.docker.container_session import ContainerOptions
from mcp_infra.exec.docker.server import ContainerExecServer
from third_party.debian_slim.rlocations import IMAGE_TAG, TARBALL
from util.oci import load_image

logger = logging.getLogger(__name__)


@asynccontextmanager
async def scratch_container(image: str) -> AsyncGenerator[MCPToolProvider]:
    """Ephemeral Docker container for agent scratch work, as MCPToolProvider.

    Container is created by ContainerExecServer's lifespan on Client entry and
    destroyed on exit. Network mode defaults to "none" (isolated).
    """
    opts = ContainerOptions(image=image)  # network_mode defaults to "none"
    async with aiodocker.Docker() as docker_client:
        server = ContainerExecServer(docker_client, opts)
        async with Client(server) as mcp_client:
            logger.info("Scratch container started (image=%s)", image)
            yield MCPToolProvider(mcp_client)
        logger.info("Scratch container stopped")


def load_scratch_image() -> str:
    """Load the debian-slim image into the local Docker daemon and return its tag."""
    load_image(TARBALL)
    return IMAGE_TAG
