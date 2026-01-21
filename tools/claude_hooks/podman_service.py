"""Podman system service management.

Starts podman system service under supervisor to provide Docker-compatible API.
"""

from __future__ import annotations

import importlib.resources
import logging
import time
from importlib.resources.abc import Traversable
from pathlib import Path

from tools.claude_hooks.supervisor_setup import SupervisorClient

logger = logging.getLogger(__name__)


def setup_podman_storage() -> None:
    """Configure podman for gVisor compatibility.

    gVisor sandbox has restrictions that require specific podman configuration:
    1. VFS storage driver (no overlay filesystem support)
    2. System-level config (/etc/containers) since running as root
    3. Explicit runroot and graphroot paths
    4. Host user namespace (userns = "host")
    """
    podman_config: Traversable = importlib.resources.files("tools.claude_hooks.config.podman")

    # Storage configuration (system-level since running as root)
    storage_conf = Path("/etc/containers/storage.conf")
    storage_conf.parent.mkdir(parents=True, exist_ok=True)
    storage_conf.write_text(podman_config.joinpath("storage.conf").read_text())

    # Container runtime configuration
    containers_conf = Path("/etc/containers/containers.conf")
    containers_conf.write_text(podman_config.joinpath("containers.conf").read_text())

    # Ensure storage directories exist
    Path("/run/containers/storage").mkdir(parents=True, exist_ok=True)
    Path("/var/lib/containers/storage").mkdir(parents=True, exist_ok=True)

    logger.info("Configured podman for gVisor: VFS storage, host userns")


def setup_podman(supervisor: SupervisorClient) -> str:
    """Set up podman storage and start service.

    Args:
        supervisor: Supervisor client for managing services

    Returns:
        Socket URL (with unix:// prefix)
    """
    logger.info("Configuring podman...")
    setup_podman_storage()
    socket_url = start_podman_service(supervisor)
    logger.info(f"Podman service started: DOCKER_HOST={socket_url}")
    return socket_url


def start_podman_service(supervisor: SupervisorClient) -> str:
    """Start podman system service under supervisor.

    Args:
        supervisor: Supervisor client for adding services

    Provides Docker-compatible API at Unix socket.
    Does NOT start infrastructure containers (PostgreSQL, Registry, Proxy).

    Returns:
        Socket URL (with unix:// prefix)

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    logger.info("Starting podman system service...")

    # Podman socket path (rootful since we're running as root)
    socket_path = Path("/run/podman/podman.sock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # Start podman system service (--time=0 means never timeout, keep running)
    supervisor.add_service(
        name="podman",
        command=f"podman system service --time=0 unix://{socket_path}",
        directory=Path.home(),
    )

    # Wait for socket to be ready
    _wait_for_socket(socket_path, supervisor, timeout=10)

    socket_url = f"unix://{socket_path}"
    logger.info(f"Podman service ready at {socket_url}")
    return socket_url


def _wait_for_socket(socket_path: Path, supervisor: SupervisorClient, timeout: int = 10) -> None:
    """Wait for Unix socket to be created and service to be running.

    Args:
        socket_path: Path to Unix socket
        supervisor: Supervisor client for checking service status
        timeout: Maximum wait time in seconds

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    for _i in range(timeout * 10):  # Check every 0.1s
        if socket_path.exists() and supervisor.is_service_running("podman", wait_for_start=False):
            return
        time.sleep(0.1)

    raise TimeoutError(f"Podman socket {socket_path} did not become ready in {timeout}s")
