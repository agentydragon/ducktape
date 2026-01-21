"""Podman system service management.

Starts podman system service under supervisor to provide Docker-compatible API.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.supervisor_setup import ProcessState, SupervisorClient

logger = logging.getLogger(__name__)

PODMAN_SERVICE = "podman"
SKIP_ENV_VAR = "CLAUDE_HOOKS_SKIP_PODMAN"


class PodmanInstallError(Exception):
    """Raised when podman installation fails."""


@dataclass
class PodmanSetup:
    """Result of podman setup."""

    socket_url: str
    supervisor: SupervisorClient

    @property
    def status(self) -> str:
        """Get human-readable podman status."""
        if self.supervisor.is_service_running(PODMAN_SERVICE, wait_for_start=False):
            return "running"
        return "not running"

    @property
    def guidance(self) -> str:
        """Get podman usage guidance for gVisor sandbox."""
        return textwrap.dedent(
            f"""\
            Podman in gVisor Sandbox
            ========================
            Podman is configured with gVisor-specific workarounds.
            Running under supervisor (status: {self.status}). DOCKER_HOST={self.socket_url}

            Use fully qualified image names (docker.io/library/...)

            Configuration Applied:
            ----------------------
            - VFS storage (/etc/containers/storage.conf)
            - userns = "host"
            - run.oci.keep_original_groups=1 annotation (auto-applied)
            - --network=host
            """
        )


def is_podman_available() -> bool:
    """Check if podman binary is available in PATH."""
    return shutil.which("podman") is not None


def install_podman() -> None:
    """Install podman via apt if not already installed.

    Raises:
        PodmanInstallError: If installation fails.
    """
    if is_podman_available():
        logger.info("Podman already installed")
        return

    logger.info("Installing podman via apt...")

    # Update apt cache
    try:
        result = subprocess.run(["apt-get", "update"], check=False, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning("apt-get update failed: %s", result.stderr)
    except subprocess.TimeoutExpired:
        logger.warning("apt-get update timed out")
    except FileNotFoundError as e:
        raise PodmanInstallError("apt-get not found, cannot install podman") from e

    # Install podman
    try:
        result = subprocess.run(
            ["apt-get", "install", "-y", "podman"], check=False, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise PodmanInstallError(f"apt-get install podman failed: {result.stderr}")
        logger.info("Podman installed successfully")
    except subprocess.TimeoutExpired as e:
        raise PodmanInstallError("podman installation timed out") from e
    except FileNotFoundError as e:
        raise PodmanInstallError("apt-get not found, cannot install podman") from e

    # Verify installation
    if not is_podman_available():
        raise PodmanInstallError("podman not found after installation")


def setup_podman_storage() -> None:
    """Configure podman for gVisor compatibility.

    gVisor sandbox has restrictions that require specific podman configuration:
    1. VFS storage driver (no overlay filesystem support)
    2. System-level config (/etc/containers) since running as root
    3. Explicit runroot and graphroot paths
    4. Host user namespace (userns = "host")
    5. Registry configuration for short image names

    Note: If there's pre-existing podman storage created with a different driver,
    podman will fail with "database graph driver X does not match our graph driver Y".
    This is intentional - we don't automatically delete existing state. To fix:
      rm -rf /var/lib/containers/storage /run/containers/storage
    See tools/claude_hooks/podman_service.py for details.
    """
    podman_config: Traversable = importlib.resources.files("tools.claude_hooks.config.podman")

    # Storage configuration (system-level since running as root)
    storage_conf = Path("/etc/containers/storage.conf")
    storage_conf.parent.mkdir(parents=True, exist_ok=True)
    storage_conf.write_text(podman_config.joinpath("storage.conf").read_text())

    # Container runtime configuration
    containers_conf = Path("/etc/containers/containers.conf")
    containers_conf.write_text(podman_config.joinpath("containers.conf").read_text())

    # Registry configuration (allows short image names like "alpine")
    registries_conf = Path("/etc/containers/registries.conf")
    registries_conf.write_text(podman_config.joinpath("registries.conf").read_text())

    # Ensure storage directories exist
    Path("/run/containers/storage").mkdir(parents=True, exist_ok=True)
    Path("/var/lib/containers/storage").mkdir(parents=True, exist_ok=True)

    logger.info("Configured podman for gVisor: VFS storage, host userns, registries")


def setup_podman(supervisor: SupervisorClient) -> PodmanSetup:
    """Set up podman storage and start service.

    If podman is not installed, attempts to install it via apt.
    Idempotent: if podman service is already running, returns immediately.

    Args:
        supervisor: Supervisor client for managing services

    Returns:
        PodmanSetup with socket URL and supervisor client

    Raises:
        SkipError: If CLAUDE_HOOKS_SKIP_PODMAN is set.
        PodmanInstallError: If podman installation fails.
    """
    if os.environ.get(SKIP_ENV_VAR):
        logger.info("Skipping podman setup (%s set)", SKIP_ENV_VAR)
        raise SkipError("Podman", SKIP_ENV_VAR)

    socket_path = Path("/run/podman/podman.sock")
    socket_url = f"unix://{socket_path}"

    # Check if podman service is already running (idempotent case)
    if _is_podman_service_healthy(supervisor, socket_path):
        logger.info("Podman service already running, skipping setup")
        return PodmanSetup(socket_url=socket_url, supervisor=supervisor)

    if not is_podman_available():
        logger.info("Podman not found, installing...")
        install_podman()

    logger.info("Configuring podman...")
    setup_podman_storage()
    socket_url = start_podman_service(supervisor)
    logger.info(f"Podman service started: DOCKER_HOST={socket_url}")
    return PodmanSetup(socket_url=socket_url, supervisor=supervisor)


def _is_podman_service_healthy(supervisor: SupervisorClient, socket_path: Path) -> bool:
    """Check if podman service is running and socket exists.

    Used for idempotency: skip setup if service is already healthy.
    """
    if not socket_path.exists():
        return False
    try:
        return supervisor.is_service_running(PODMAN_SERVICE, wait_for_start=False)
    except Exception:
        return False


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
        name="podman", command=f"podman system service --time=0 unix://{socket_path}", directory=Path.home()
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
        TimeoutError: If socket doesn't become ready in time, with diagnostic info
    """
    last_state = None
    for _i in range(timeout * 10):  # Check every 0.1s
        if socket_path.exists() and supervisor.is_service_running("podman", wait_for_start=False):
            return
        # Track service state for diagnostics
        try:
            info = supervisor.get_process_info("podman")
            last_state = info.statename if info else "unknown"
        except Exception:
            last_state = "error"
        time.sleep(0.1)

    # Build diagnostic message
    socket_exists = socket_path.exists()
    diag = f"socket_exists={socket_exists}, last_service_state={last_state}"

    # Log full process info at error level for visibility
    if last_state in (ProcessState.FATAL, ProcessState.BACKOFF, ProcessState.EXITED):
        try:
            info = supervisor.get_process_info("podman")
            logger.error("Podman service failed: %s", info.model_dump())
        except Exception:
            pass

    # Common cause: storage driver mismatch from pre-existing state
    hint = (
        "Common cause: storage driver mismatch. "
        "If podman was previously used with a different driver, run: "
        "rm -rf /var/lib/containers/storage /run/containers/storage"
    )
    raise TimeoutError(f"Podman socket {socket_path} did not become ready in {timeout}s ({diag}). {hint}")
