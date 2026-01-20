"""Podman system service management.

Starts podman system service under supervisor to provide Docker-compatible API.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tools.claude_hooks import supervisor_setup

logger = logging.getLogger(__name__)


def start_podman_service() -> Path:
    """Start podman system service under supervisor.

    Provides Docker-compatible API at Unix socket.
    Does NOT start infrastructure containers (PostgreSQL, Registry, Proxy).

    Returns:
        Path to podman socket

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    logger.info("Starting podman system service...")

    # Ensure supervisor is running
    supervisor_setup.start()

    # Podman socket path (rootful since we're running as root)
    socket_path = Path("/run/podman/podman.sock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # Start podman system service
    # --time=0 means never timeout (keep running)
    command = f"podman system service --time=0 unix://{socket_path}"

    supervisor_setup.add_service(name="podman", command=command, directory=Path.home())

    # Wait for socket to be ready
    _wait_for_socket(socket_path, timeout=10)

    logger.info(f"Podman service ready at {socket_path}")
    return socket_path


def _wait_for_socket(socket_path: Path, timeout: int = 10) -> None:
    """Wait for Unix socket to be created and service to be running.

    Args:
        socket_path: Path to Unix socket
        timeout: Maximum wait time in seconds

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    for _i in range(timeout * 10):  # Check every 0.1s
        if socket_path.exists() and supervisor_setup.is_service_running("podman", wait_for_start=False):
            return
        time.sleep(0.1)

    raise TimeoutError(f"Podman socket {socket_path} did not become ready in {timeout}s")


def get_status() -> str:
    """Get podman service status.

    Returns:
        Status string ("running" or "not running")
    """
    if supervisor_setup.is_service_running("podman", wait_for_start=False):
        return "running"
    return "not running"
