"""Docker scratch space for eval agents.

Uses the existing ContainerExecServer (from mcp_infra) in-process via FastMCP Client.
The server registers an `exec` tool and manages container lifecycle via its lifespan.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastmcp.client import Client

from agent_core.mcp_provider import MCPToolProvider
from skills.info_gathering.evals.docker_exec import scratch_exec_server
from third_party.containers.rlocations import DEBIAN_SLIM
from util.oci import load_oci_image

logger = logging.getLogger(__name__)


@asynccontextmanager
async def scratch_container(image: str) -> AsyncGenerator[MCPToolProvider]:
    """Ephemeral Docker container for agent scratch work, as MCPToolProvider.

    Container is created by ContainerExecServer's lifespan on Client entry and
    destroyed on exit. Network mode defaults to "none" (isolated).
    """
    async with scratch_exec_server(image) as server, Client(server) as mcp_client:
        logger.info("Scratch container started (image=%s)", image)
        yield MCPToolProvider(mcp_client)
    logger.info("Scratch container stopped")


def load_scratch_image() -> str:
    """Load the debian-slim image into the local Docker daemon and return its tag."""
    return load_oci_image(DEBIAN_SLIM)
