"""Container runtime management (Docker) for gVisor environments.

Unified setup for Docker under supervisor, exposing a Docker-compatible Unix
socket.  Docker is the preferred (and only supported) runtime:

- No runtime wrapper needed (runc handles gVisor better than crun)
- No registry/policy configuration needed
- BuildKit handles large output better than buildah

Key findings from Docker evaluation (2026-02-17):
- Works with just 4 daemon.json settings
- Layer limit: ~35 layers (kernel mount option page size, not Docker-specific)
- Disable iptables, use tmpfs data-root, disable bridge networking
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS
from devinfra.claude.errors import SkipError
from devinfra.claude.managed_files import write_config
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings
from devinfra.claude.supervisor.client import ProcessInfo, ProcessState, SupervisorClient
from devinfra.claude.supervisor.service_utils import log_service_failure, wait_for_service_socket

logger = logging.getLogger(__name__)

DOCKER_SERVICE = "dockerd"
DEFAULT_DOCKER_SOCKET = Path("/var/run/docker.sock")


@dataclass
class ContainerRuntimeSetup:
    """Result of Docker runtime setup."""

    socket_url: str
    status: str
    storage_driver: str
    env_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class _RuntimeSpec:
    """Internal: runtime-specific config produced after writing config files.

    Parameterizes the shared service lifecycle (start, health-check, etc.).
    """

    service_name: str
    socket_path: Path
    supervisor_command: str
    # Env vars exported to the session env file (DOCKER_HOST, etc.)
    client_env_vars: dict[str, str]
    # Extra env vars injected into the daemon process beyond proxy vars.
    daemon_extra_env: dict[str, str]
    storage_driver: str


# ============================================================================
# Shared lifecycle helpers
# ============================================================================


def _collect_proxy_env() -> dict[str, str]:
    """Collect proxy and SSL CA env vars from the current environment."""
    return {var: v for var in PROXY_ENV_VARS + SSL_CA_ENV_VARS if (v := os.environ.get(var))}


async def _snapshot_status(supervisor: SupervisorClient, service_name: str) -> str:
    """Return supervisor statename for a service, or UNKNOWN on error."""
    try:
        info = await supervisor.get_process_info(service_name)
        return info.statename
    except Exception:
        return ProcessState.UNKNOWN


async def _is_service_healthy(supervisor: SupervisorClient, service_name: str, socket_path: Path) -> bool:
    """Return True if the service is running in supervisor and its socket exists."""
    if not await asyncio.to_thread(socket_path.exists):
        return False
    try:
        return await supervisor.is_service_running(service_name)
    except Exception:
        return False


async def _is_docker_socket_responsive(socket_path: Path) -> bool:
    """Return True if a dockerd is already listening on the socket.

    Handles the case where the environment injects a pre-existing dockerd
    (e.g. the Claude Code web sandbox starts with one) that is not managed
    by this session's supervisor instance.
    """
    if not await asyncio.to_thread(socket_path.exists):
        return False
    try:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket_path)), timeout=1.0) as client:
            response = await client.get("http://localhost/_ping")
            return response.status_code == 200
    except (OSError, httpx.HTTPError, httpx.TransportError):
        return False


async def _start_service(
    spec: _RuntimeSpec, supervisor: SupervisorClient, on_failure: Callable[[ProcessInfo], None] | None = None
) -> None:
    """Start a container runtime under supervisor and wait for the socket.

    Merges proxy/SSL env vars into the daemon environment so the daemon can
    pull images through the TLS-inspecting egress proxy.
    """
    daemon_env = dict(spec.daemon_extra_env)
    daemon_env.update(_collect_proxy_env())

    await supervisor.add_service(
        name=spec.service_name, command=spec.supervisor_command, directory=Path.home(), environment=daemon_env
    )

    async with asyncio.timeout(10):
        await wait_for_service_socket(
            supervisor=supervisor,
            service_name=spec.service_name,
            socket_path=spec.socket_path,
            on_failure=on_failure or (lambda info: log_service_failure(spec.service_name, info)),
        )


# ============================================================================
# Docker
# ============================================================================


def _configure_docker(paths: SessionPaths, tmpfs_mounted: bool) -> _RuntimeSpec:
    """Write daemon.json and return the runtime spec for Docker.

    Configuration requirements for gVisor:
    1. Disable iptables (gVisor doesn't support nftables)
    2. Use tmpfs for data-root (9p doesn't support overlay mounts)
    3. Disable bridge networking (only host networking works in gVisor)
    """
    docker_dir = paths.docker_dir
    docker_dir.mkdir(parents=True, exist_ok=True)

    data_root = paths.container_storage_dir
    driver = "overlay" if tmpfs_mounted else "vfs"
    data_root.mkdir(parents=True, exist_ok=True)
    logger.info("Using %s storage at %s", driver, data_root)

    daemon_config = {"iptables": False, "ip6tables": False, "data-root": str(data_root), "bridge": "none"}
    config_path = docker_dir / "daemon.json"
    write_config(config_path, json.dumps(daemon_config, indent=2), "daemon.json", canary=False)

    socket_url = f"unix://{DEFAULT_DOCKER_SOCKET}"
    return _RuntimeSpec(
        service_name=DOCKER_SERVICE,
        socket_path=DEFAULT_DOCKER_SOCKET,
        supervisor_command=f"dockerd --config-file={config_path}",
        client_env_vars={"DOCKER_HOST": socket_url},
        daemon_extra_env={},
        storage_driver=driver,
    )


# ============================================================================
# Unified entry point
# ============================================================================


def get_storage_dir(paths: SessionPaths, settings: HookSettings) -> Path | None:
    """Return the shared container storage directory for tmpfs mounting, or None if disabled."""
    if not settings.setup_docker:
        return None
    return paths.container_storage_dir


def _cleanup_stale_docker_pid() -> None:
    """Remove /var/run/docker.pid if it refers to a non-dockerd process.

    Handles the case where a previous session's PID file persists and its PID
    has since been reused by an unrelated process (e.g. supervisord).  Without
    this cleanup dockerd refuses to start, mistaking that PID for a live daemon.
    """
    pid_file = Path("/var/run/docker.pid")
    sock_file = DEFAULT_DOCKER_SOCKET

    logger.info("docker socket check: exists=%s", sock_file.exists())

    if not pid_file.exists():
        logger.info("No /var/run/docker.pid found")
        return

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError) as e:
        logger.warning("Failed to read /var/run/docker.pid: %s", e)
        return

    logger.info("Found /var/run/docker.pid with pid=%d", pid)

    comm_path = Path(f"/proc/{pid}/comm")
    if comm_path.exists():
        try:
            comm = comm_path.read_text().strip()
            logger.info("PID %d comm: %s", pid, comm)
        except OSError as e:
            logger.warning("Failed to read comm for PID %d: %s", pid, e)
            comm = ""
    else:
        comm = ""

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace").strip()
            logger.info("PID %d cmdline: %s", pid, cmdline)
        except OSError as e:
            logger.warning("Failed to read cmdline for PID %d: %s", pid, e)

    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith(("Name:", "PPid:", "State:")):
                    logger.info("PID %d status: %s", pid, line)
        except OSError as e:
            logger.warning("Failed to read status for PID %d: %s", pid, e)

    # If the PID exists but isn't dockerd, the pid file is stale — remove it so
    # dockerd can start.  If the process no longer exists at all, also remove it.
    is_dockerd = comm == "dockerd"
    pid_exists = Path(f"/proc/{pid}").exists()
    if not pid_exists or not is_dockerd:
        logger.info("Removing stale /var/run/docker.pid (pid=%d, pid_exists=%s, comm=%r)", pid, pid_exists, comm)
        try:
            pid_file.unlink()
        except OSError as e:
            logger.warning("Failed to remove /var/run/docker.pid: %s", e)


async def setup_container_runtime(
    paths: SessionPaths, settings: HookSettings, supervisor: SupervisorClient, tmpfs_mounted: bool
) -> ContainerRuntimeSetup:
    """Set up Docker under supervisor.

    Idempotent: if the service is already running, skips the start step and
    returns the current state.

    Raises:
        SkipError: If setup_docker is False.
    """
    if not settings.setup_docker:
        raise SkipError("Docker setup disabled (setup_docker=False)")

    spec = _configure_docker(paths, tmpfs_mounted)

    socket_url = f"unix://{spec.socket_path}"

    if await _is_service_healthy(supervisor, spec.service_name, spec.socket_path):
        logger.info("%s service already running, skipping setup", spec.service_name)
        status = await _snapshot_status(supervisor, spec.service_name)
        return ContainerRuntimeSetup(
            socket_url=socket_url, status=status, storage_driver=spec.storage_driver, env_vars=spec.client_env_vars
        )

    # Check for a pre-existing daemon not managed by this supervisor (e.g. the
    # Claude Code web sandbox injects a dockerd before the hook runs).
    if await _is_docker_socket_responsive(spec.socket_path):
        logger.info("Pre-existing dockerd responsive on %s, using it", spec.socket_path)
        return ContainerRuntimeSetup(
            socket_url=socket_url,
            status="pre-existing",
            storage_driver=spec.storage_driver,
            env_vars=spec.client_env_vars,
        )

    # Clean up stale /var/run/docker.pid before attempting start.  A leftover
    # pid file whose PID has been reused by a non-dockerd process (e.g.
    # supervisord) causes dockerd to abort with "process still running".
    _cleanup_stale_docker_pid()

    logger.info("Configuring %s...", spec.service_name)

    await _start_service(spec, supervisor)

    logger.info("%s service started: DOCKER_HOST=%s", spec.service_name, socket_url)
    status = await _snapshot_status(supervisor, spec.service_name)
    return ContainerRuntimeSetup(
        socket_url=socket_url, status=status, storage_driver=spec.storage_driver, env_vars=spec.client_env_vars
    )
