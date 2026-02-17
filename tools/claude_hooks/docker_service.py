"""Docker daemon management for gVisor environments.

Starts dockerd under supervisor with minimal configuration optimized for gVisor.

Key findings from evaluation (2026-02-17):
- Docker works better than Podman in gVisor (90% fewer workarounds needed)
- No runtime wrapper needed (runc handles setgroups/freezer gracefully)
- No registry configuration needed (short names work by default)
- No image signature policy needed
- BuildKit handles large output better than buildah (no SIGPIPE)

Configuration requirements:
1. Disable iptables (gVisor doesn't support nftables)
2. Use tmpfs for storage (9p doesn't support overlay mounts)
3. Disable bridge networking (only host networking available in gVisor)

Layer limit: ~35 layers (kernel mount option page size limit, not Docker-specific)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.managed_files import write_config
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import ProcessState, SupervisorClient
from tools.claude_hooks.supervisor.service_utils import log_service_failure, wait_for_service_socket

logger = logging.getLogger(__name__)

DOCKER_SERVICE = "dockerd"
DEFAULT_SOCKET_PATH = Path("/var/run/docker.sock")


@dataclass
class DockerSetup:
    """Result of Docker setup."""

    socket_url: str
    status: str
    storage_driver: str = "overlayfs"
    env_vars: dict[str, str] = None

    def __post_init__(self) -> None:
        if self.env_vars is None:
            self.env_vars = {}


def setup_docker_config(settings: HookSettings, tmpfs_root: Path | None) -> tuple[Path, str]:
    """Generate minimal daemon.json for gVisor compatibility.

    Args:
        settings: Hook settings.
        tmpfs_root: Path to exec-capable tmpfs mount. Required for overlay storage
            (9p filesystem doesn't support overlay mounts).

    Returns:
        Tuple of (config_path, storage_driver)
    """
    docker_dir = settings.get_docker_dir()
    docker_dir.mkdir(parents=True, exist_ok=True)

    # Choose storage location based on tmpfs availability
    if tmpfs_root is not None:
        data_root = tmpfs_root / "docker"
        driver = "overlay"
    else:
        data_root = docker_dir / "data"
        driver = "vfs"

    data_root.mkdir(parents=True, exist_ok=True)
    logger.info("Using %s storage at %s", driver, data_root)

    # Minimal daemon.json for gVisor
    daemon_config = {
        # Disable iptables - gVisor doesn't support nftables
        "iptables": False,
        "ip6tables": False,
        # Use tmpfs for storage (9p doesn't support overlay)
        "data-root": str(data_root),
        # Disable bridge networking (only host networking works in gVisor)
        "bridge": "none",
    }

    config_path = docker_dir / "daemon.json"
    write_config(config_path, json.dumps(daemon_config, indent=2), "daemon.json")

    return config_path, driver


async def _snapshot_docker_status(supervisor: SupervisorClient) -> str:
    """Snapshot dockerd supervisor process status."""
    try:
        info = await supervisor.get_process_info(DOCKER_SERVICE)
        return info.statename
    except Exception:
        return ProcessState.UNKNOWN


async def _is_docker_service_healthy(supervisor: SupervisorClient) -> bool:
    """Check if dockerd service is running and socket exists."""
    if not DEFAULT_SOCKET_PATH.exists():
        return False
    try:
        return await supervisor.is_service_running(DOCKER_SERVICE)
    except Exception:
        return False


async def setup_docker(settings: HookSettings, supervisor: SupervisorClient, tmpfs_root: Path | None) -> DockerSetup:
    """Set up Docker daemon with minimal gVisor-compatible configuration.

    Evaluation findings (2026-02-17):
    - Works with just 4 daemon.json settings (vs Podman's 8+ config files)
    - No runtime wrapper needed (runc handles gVisor better than crun)
    - No setgroups annotation needed
    - No cgroup freezer workarounds needed
    - 85% less code than Podman setup

    Args:
        settings: Hook settings.
        supervisor: Supervisor client for process management.
        tmpfs_root: Path to exec-capable tmpfs. If provided, Docker uses
            overlay storage. If None, falls back to VFS (slower, no layer caching).

    Raises:
        SkipError: If install_docker is False in settings.
    """
    if settings.container_runtime != "docker":
        logger.info("Skipping Docker setup (container_runtime=%s)", settings.container_runtime)
        raise SkipError("Docker")

    socket_url = f"unix://{DEFAULT_SOCKET_PATH}"

    # Check if Docker service is already running (idempotent case)
    if await _is_docker_service_healthy(supervisor):
        logger.info("Docker service already running, skipping setup")
        status = await _snapshot_docker_status(supervisor)
        return DockerSetup(socket_url=socket_url, status=status, env_vars={"DOCKER_HOST": socket_url})

    logger.info("Configuring dockerd...")
    config_path, storage_driver = setup_docker_config(settings, tmpfs_root=tmpfs_root)

    logger.info("Starting dockerd service...")
    env_vars = await start_docker_service(settings, supervisor, config_path)

    logger.info("Docker service started: DOCKER_HOST=%s", env_vars["DOCKER_HOST"])
    status = await _snapshot_docker_status(supervisor)
    return DockerSetup(socket_url=socket_url, status=status, storage_driver=storage_driver, env_vars=env_vars)


async def start_docker_service(
    settings: HookSettings, supervisor: SupervisorClient, config_path: Path
) -> dict[str, str]:
    """Start dockerd system service under supervisor.

    Returns:
        Dict of env vars including DOCKER_HOST

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    socket_url = f"unix://{DEFAULT_SOCKET_PATH}"

    # The dockerd daemon runs under supervisor which doesn't inherit the
    # container's env vars. Merge proxy/SSL vars from the current environment
    # so the daemon can pull images through the TLS-inspecting egress proxy.
    daemon_env = {}
    for var in PROXY_ENV_VARS + SSL_CA_ENV_VARS:
        if value := os.environ.get(var):
            daemon_env[var] = value

    # Start dockerd with minimal config
    await supervisor.add_service(
        name=DOCKER_SERVICE,
        command=f"dockerd --config-file={config_path}",
        directory=Path.home(),
        environment=daemon_env,
    )

    # Wait for socket to be ready
    async with asyncio.timeout(10):
        await wait_for_service_socket(
            supervisor=supervisor,
            service_name=DOCKER_SERVICE,
            socket_path=DEFAULT_SOCKET_PATH,
            on_failure=lambda info: log_service_failure("Docker", info),
        )

    logger.info("Docker service ready at %s", socket_url)
    return {"DOCKER_HOST": socket_url}
