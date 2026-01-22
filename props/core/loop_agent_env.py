"""Simplified agent environment for in-container agent loops.

This is the new architecture where:
- The agent loop runs inside the container (CMD, not /init)
- The container talks to the LLM proxy (not HTTP MCP server)
- Tools are executed via subprocess (not docker_exec from host)
- The container exits 0 on success, non-zero on failure

Host scaffold responsibilities:
1. Create temporary database user with RLS scoping
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
from props.db.temp_user_manager import TempUserManager

if TYPE_CHECKING:
    import aiodocker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerResult:
    """Result of running an agent container."""

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

    This function:
    1. Creates a temporary database user
    2. Starts the container with proper environment
    3. Waits for container to exit
    4. Captures logs and returns result
    5. Cleans up temp user and container

    The container should run its agent loop via CMD and exit 0 on success.

    Args:
        docker_client: Docker client instance
        agent_run_id: UUID for this agent run
        db_config: Database configuration
        image: OCI image reference (e.g., "localhost:5050/critic@sha256:...")
        llm_proxy_url: URL of the LLM proxy (e.g., "http://props-proxy:5050")
        timeout_seconds: Max seconds before container is killed (None = no timeout, for daemons)
        extra_env: Additional environment variables for the container
        container_name: Optional container name (defaults to agent-{short_uuid})
        extra_hosts: Additional host mappings (e.g., {"host.docker.internal": "host-gateway"})

    Returns:
        ContainerResult with exit_code, stdout, stderr (exit_code=-1 on timeout)

    Example:
        result = await run_loop_agent(
            docker_client=docker_client,
            agent_run_id=run_id,
            db_config=db_config,
            image="localhost:5050/critic@sha256:...",
            llm_proxy_url="http://props-proxy:5050",
            timeout_seconds=3600,
        )
        if result.exit_code == 0:
            logger.info("Agent completed successfully")
        elif result.exit_code == -1:
            logger.error("Agent timed out")
        else:
            logger.error("Agent failed: %s", result.stderr)
    """
    # Resolve image from OCI reference
    image_id = await resolve_image_ref_async(docker_client, image)
    logger.info("Using image %s from %s", image_id[:19], image)

    # Create temporary database user
    async with TempUserManager(db_config.admin, agent_run_id) as temp_creds:
        logger.info("Created temporary database user: %s", temp_creds.username)

        container = None
        try:
            # Build container config
            name = container_name or f"agent-{short_uuid(agent_run_id)}"
            container_db = db_config.for_container_user(temp_creds.username, temp_creds.password)

            env = {
                # Database credentials (agent derives run ID from PGUSER via current_agent_run_id())
                "PGHOST": container_db.host,
                "PGPORT": str(container_db.port),
                "PGUSER": container_db.user,
                "PGPASSWORD": container_db.password,
                "PGDATABASE": container_db.database,
                # LLM proxy credentials (same password as database)
                "OPENAI_BASE_URL": f"{llm_proxy_url}/v1",
                "OPENAI_API_KEY": temp_creds.password,
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
