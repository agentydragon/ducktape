"""Simplified agent environment for in-container agent loops.

This is the new architecture where:
- The agent loop runs inside the container (CMD, not /init)
- The container talks to the LLM proxy (not HTTP MCP server)
- Tools are executed via subprocess (not docker_exec from host)
- The container exits 0 on success, non-zero on failure

Host scaffold responsibilities:
1. Ensure agent database role exists with RLS scoping
2. Start container with:
   - OPENAI_BASE_URL pointing to LLM proxy
   - OPENAI_API_KEY = temp user password
   - PG* env vars for database access
3. Wait for container exit
4. Capture and store container logs
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from props.core.display import short_uuid
from props.core.docker_env import PROPS_NETWORK_NAME
from props.core.oci_utils import resolve_image_ref_async
from props.db.config import DatabaseConfig
from props.orchestration.agent_credentials import ensure_agent_role

if TYPE_CHECKING:
    import aiodocker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerResult:
    exit_code: int
    stdout: str
    stderr: str


async def run_loop_agent(
    docker_client: aiodocker.Docker,
    agent_run_id: UUID,
    db_config: DatabaseConfig,
    *,
    image: str,
    llm_proxy_url: str,
    timeout_seconds: int | None = None,
    extra_env: dict[str, str] | None = None,
    container_name: str | None = None,
    extra_hosts: dict[str, str] | None = None,
) -> ContainerResult:
    """Run an agent container with in-container agent loop.

    Ensures agent role exists, starts container, waits for exit, captures logs, cleans up.
    Container should run its agent loop via CMD and exit 0 on success.
    timeout_seconds=None means no timeout (for daemons). Returns exit_code=-1 on timeout.
    """
    # Resolve image from OCI reference
    image_id = await resolve_image_ref_async(docker_client, image)
    logger.info("Using image %s from %s", image_id[:19], image)

    # Ensure agent database role exists
    creds = await ensure_agent_role(db_config.admin, agent_run_id)
    logger.info("Agent role ready: %s", creds.username)

    container = None
    try:
        # Build container config
        name = container_name or f"agent-{short_uuid(agent_run_id)}"
        container_db = db_config.for_container_user(creds.username, creds.password)

        env = {
            # Database credentials (agent derives run ID from PGUSER via current_agent_run_id())
            "PGHOST": container_db.host,
            "PGPORT": str(container_db.port),
            "PGUSER": container_db.user,
            "PGPASSWORD": container_db.password,
            "PGDATABASE": container_db.database,
            # LLM proxy credentials (same password as database)
            "OPENAI_BASE_URL": f"{llm_proxy_url}/v1",
            "OPENAI_API_KEY": creds.password,
        }
        if extra_env:
            env.update(extra_env)

        # Create and start container
        host_config: dict[str, object] = {
            "NetworkMode": PROPS_NETWORK_NAME,
            "AutoRemove": False,  # Keep container to read logs
        }
        if extra_hosts:
            # Convert {"host": "ip"} to ["host:ip"] format for Docker API
            host_config["ExtraHosts"] = [f"{host}:{ip}" for host, ip in extra_hosts.items()]

        container_config: dict[str, object] = {
            "Image": image_id,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": host_config,
            "Labels": {"adgn.project": "props", "adgn.agent_run_id": str(agent_run_id)},
        }

        container = await docker_client.containers.create(container_config, name=name)  # type: ignore[arg-type]
        logger.info("Created container %s", name)

        await container.start()
        logger.info("Started container %s", name)

        # Wait for container to exit (with optional timeout)
        timed_out = False
        try:
            if timeout_seconds is not None:
                exit_info = await asyncio.wait_for(container.wait(), timeout=timeout_seconds)
            else:
                exit_info = await container.wait()
            exit_code = exit_info.get("StatusCode", 1)
        except TimeoutError:
            logger.error("Container %s timed out after %d seconds", name, timeout_seconds)
            timed_out = True
            exit_code = -1  # Sentinel for timeout
            # Kill the container
            try:
                await container.kill()
            except Exception as e:
                logger.warning("Failed to kill timed-out container: %s", e)

        if not timed_out:
            logger.info("Container %s exited with code %d", name, exit_code)

        # Capture logs
        stdout_logs = await container.log(stdout=True, stderr=False)
        stderr_logs = await container.log(stdout=False, stderr=True)

        stdout = "".join(stdout_logs) if stdout_logs else ""
        stderr = "".join(stderr_logs) if stderr_logs else ""

        return ContainerResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    finally:
        # Clean up container
        if container is not None:
            try:
                await container.delete(force=True)
                logger.info("Deleted container")
            except Exception as e:
                logger.warning("Failed to delete container: %s", e)
