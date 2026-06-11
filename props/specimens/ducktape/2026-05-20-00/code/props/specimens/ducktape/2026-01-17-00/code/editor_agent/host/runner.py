from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiodocker
from fastmcp.server.auth import StaticTokenVerifier

from editor_agent.host.submit_server import EditorSubmitServer
from mcp_infra.compositor.resources_server import ResourcesServer
from mcp_infra.compositor.server import Compositor
from mcp_infra.constants import WORKING_DIR
from mcp_infra.exec.container_session import ContainerOptions
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.mounted import Mounted
from net_util.docker import get_docker_network_gateway_async
from net_util.net import pick_free_port

# TODO: The pattern of "create token + StaticTokenVerifier, start FastMCP on free port,
# get docker gateway IP, pass MCP_SERVER_URL/TOKEN to container" is duplicated in
# props/core/agent_setup.py. Extract to agent_pkg as shared infrastructure.

DEFAULT_NETWORK = "bridge"


@dataclass
class EditorDockerSession:
    submit_server: EditorSubmitServer
    container_server: ContainerExecServer
    compositor: Compositor
    runtime: Mounted[ContainerExecServer]
    resources: Mounted[ResourcesServer]
    filename: str
    original_content: str | None = None
    _server_task: asyncio.Task[None] | None = None

    async def shutdown(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._server_task
        await self.compositor.__aexit__(None, None, None)


def writeback_success(host_file: Path, content: str) -> None:
    """Write submitted content verbatim to the host file."""
    host_file.write_text(content, encoding="utf-8")


@asynccontextmanager
async def editor_docker_session(
    *, file_path: Path, prompt: str, docker_client: aiodocker.Docker, image_id: str, network_name: str = DEFAULT_NETWORK
) -> AsyncIterator[EditorDockerSession]:
    """Create a docker-exec + submit-server session for a single file.

    - Reads file content into memory (not mounted).
    - Starts submit MCP server reachable from the container via streamable HTTP.
    - Container runs the editor agent image (with /init baked in).
    """
    original_content = file_path.read_text(encoding="utf-8")
    filename = file_path.name

    # Create submit server with bearer token auth
    token = secrets.token_hex(32)
    auth = StaticTokenVerifier({token: {"client_id": "editor", "scopes": []}})
    submit_server = EditorSubmitServer(original_content=original_content, filename=filename, prompt=prompt, auth=auth)

    # Start FastMCP server on a free port
    port = pick_free_port()
    gateway_ip = await get_docker_network_gateway_async(docker_client, network_name)
    url_for_container = f"http://{gateway_ip}:{port}/mcp"

    server_task = asyncio.create_task(submit_server.run_async(transport="streamable-http", host="0.0.0.0", port=port))

    # Give server a moment to start
    await asyncio.sleep(0.1)

    env = {"MCP_SERVER_URL": url_for_container, "MCP_SERVER_TOKEN": token}

    opts = ContainerOptions(
        image=image_id, working_dir=WORKING_DIR, binds=[], network_mode=network_name, environment=env
    )

    compositor = Compositor()
    await compositor.__aenter__()
    resources_mount = compositor.resources

    container_server = ContainerExecServer(docker_client, opts)
    runtime_mount = await compositor.mount_inproc(
        ContainerExecServer.DOCKER_MOUNT_PREFIX, container_server, pinned=True
    )

    session = EditorDockerSession(
        submit_server=submit_server,
        container_server=container_server,
        compositor=compositor,
        runtime=runtime_mount,
        resources=resources_mount,
        filename=filename,
        original_content=original_content,
        _server_task=server_task,
    )
    try:
        yield session
    finally:
        await session.shutdown()
