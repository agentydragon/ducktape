"""Scratch container exec MCP server for agent computation.

Wraps mcp_infra's ContainerExecServer — a full MCP server providing an `exec` tool
with proper stream handling, timeouts, and output formatting. The server is yielded
so each framework can consume it in its native way (e.g., PydanticAI's FastMCPToolset,
or via fastmcp.Client for others).
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiodocker

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig

logger = logging.getLogger(__name__)


def _proxy_env() -> dict[str, str]:
    """Collect HTTP(S) proxy env vars for container networking."""
    env: dict[str, str] = {}
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        if val := os.environ.get(var):
            env[var] = val
    return env


@asynccontextmanager
async def scratch_exec_server(
    image: str = "python:3.13-slim", *, binds: list[BindMount] | None = None, working_dir: Path = Path("/tmp")
) -> AsyncGenerator[ContainerExecServer]:
    """Create a scratch container with an MCP exec tool server.

    The server exposes an `exec` tool with cmd (list[str]) and timeout_ms (int).
    cwd is hidden from the model and pinned to `working_dir`. User and env
    fields are disabled. Uses host networking and proxy env vars for
    internet access. Optional `binds` are mounted into the container.
    """
    async with aiodocker.Docker() as docker_client:
        server = ContainerExecServer(
            docker_client,
            ContainerExecServerConfig(
                image=image,
                working_dir=working_dir,
                network_mode="host",
                environment=_proxy_env(),
                allow_user_field=False,
                allow_env_field=False,
                cwd_policy=AlwaysSetTo(value=working_dir),
                binds=list(binds or []),
            ),
        )
        logger.info(
            "Scratch exec server created (image=%s, network=host, working_dir=%s, binds=%d)",
            image,
            working_dir,
            len(binds or []),
        )
        yield server
