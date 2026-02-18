"""Container runtime management (Docker or Podman) for gVisor environments.

Unified setup for Docker and Podman under supervisor.  Both runtimes expose a
Docker-compatible Unix socket.  Docker is the preferred runtime:

- No runtime wrapper needed (runc handles gVisor better than crun)
- No registry/policy configuration needed
- BuildKit handles large output better than buildah

Podman is available as an alternative with additional gVisor workarounds:

- crun-gvisor-wrapper for setgroups/freezer compatibility
- Extra config files (containers.conf, registries.conf, policy.json)
- CONTAINERS_* env vars to point at the session-local config

Key findings from Docker evaluation (2026-02-17):
- Works with just 4 daemon.json settings vs Podman's 8+ config files
- Layer limit: ~35 layers (kernel mount option page size, not Docker-specific)
- Disable iptables, use tmpfs data-root, disable bridge networking
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import json
import logging
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

import httpx

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.managed_files import write_config
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import ProcessInfo, ProcessState, SupervisorClient
from tools.claude_hooks.supervisor.service_utils import log_service_failure, wait_for_service_socket

logger = logging.getLogger(__name__)

DOCKER_SERVICE = "dockerd"
PODMAN_SERVICE = "podman"
DEFAULT_DOCKER_SOCKET = Path("/var/run/docker.sock")


@dataclass
class ContainerRuntimeSetup:
    """Result of container runtime setup (Docker or Podman)."""

    runtime: Literal["docker", "podman"]
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
    # Env vars exported to the session env file (DOCKER_HOST, CONTAINERS_*, etc.)
    client_env_vars: dict[str, str]
    # Extra env vars injected into the daemon process beyond proxy vars.
    # For podman: CONTAINERS_* (daemon needs them to find its config).
    # For docker: empty (only proxy vars are needed).
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
    if not socket_path.exists():
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
    if not socket_path.exists():
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


def _configure_docker(settings: HookSettings, tmpfs_mounted: bool) -> _RuntimeSpec:
    """Write daemon.json and return the runtime spec for Docker.

    Configuration requirements for gVisor:
    1. Disable iptables (gVisor doesn't support nftables)
    2. Use tmpfs for data-root (9p doesn't support overlay mounts)
    3. Disable bridge networking (only host networking works in gVisor)
    """
    docker_dir = settings.get_docker_dir()
    docker_dir.mkdir(parents=True, exist_ok=True)

    data_root = settings.get_container_storage_dir()
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
# Podman
# ============================================================================


class PodmanInstallError(Exception):
    """Raised when podman installation fails."""


async def install_podman() -> None:
    """Install podman and crun via apt.

    Raises:
        FileNotFoundError: If apt-get is not available.
        TimeoutError: If apt operations time out.
        PodmanInstallError: If installation fails.
    """
    logger.info("Installing podman via apt...")

    process = await asyncio.create_subprocess_exec(
        "apt-get", "update", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    if process.returncode != 0:
        logger.warning("apt-get update failed: %s", stderr.decode())

    process = await asyncio.create_subprocess_exec(
        "apt-get", "install", "-y", "podman", "crun", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    if process.returncode != 0:
        raise PodmanInstallError(f"apt-get install podman crun failed: {stderr.decode()}")

    if shutil.which("podman") is None:
        raise PodmanInstallError("podman not found after installation")
    logger.info("Podman and crun installed successfully")


def _get_podman_socket_path(settings: HookSettings) -> Path:
    """Return the podman Unix socket path.

    Unix sockets have a 108-character path limit (UNIX_PATH_MAX).  When
    XDG_CACHE_HOME is deeply nested (e.g. in Bazel test environments) the
    natural path can exceed this limit, so we use a short /tmp path with a
    hash for uniqueness.
    """
    if settings.podman_socket is not None:
        return settings.podman_socket
    dir_hash = hashlib.sha256(str(settings.get_podman_dir()).encode()).hexdigest()[:12]
    return Path(f"/tmp/claude-podman-{dir_hash}.sock")


def _install_crun_wrapper(podman_dir: Path, podman_config: Traversable) -> Path:
    wrapper_path = podman_dir / "crun-gvisor-wrapper"
    write_config(wrapper_path, podman_config.joinpath("crun_gvisor_wrapper.py").read_text(), "crun-gvisor-wrapper")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper_path


def _configure_podman(settings: HookSettings, tmpfs_mounted: bool) -> _RuntimeSpec:
    """Write podman config files and return the runtime spec.

    Writes containers.conf, registries.conf, policy.json, storage.conf, and the
    crun-gvisor-wrapper under session_dir/podman/.  Uses CONTAINERS_* env vars
    to isolate podman from any system config.

    gVisor sandbox restrictions require:
    1. Overlay on tmpfs (preferred): supports xattr, enables layer caching.
       Falls back to VFS on 9p if tmpfs is unavailable.
    2. Host user namespace (userns = "host")
    3. run.oci.keep_original_groups=1 annotation (via crun-gvisor-wrapper)
    """
    podman_dir = settings.get_podman_dir()
    podman_dir.mkdir(parents=True, exist_ok=True)

    podman_config: Traversable = importlib.resources.files("tools.claude_hooks.config.podman")

    storage_root = settings.get_container_storage_dir()
    driver = "overlay" if tmpfs_mounted else "vfs"
    storage_dir = storage_root / "storage"
    runroot_dir = storage_root / "run"
    storage_dir.mkdir(parents=True, exist_ok=True)
    runroot_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Using %s storage at %s", driver, storage_dir)

    write_config(
        podman_dir / "storage.conf",
        f'[storage]\ndriver = "{driver}"\nrunroot = "{runroot_dir}"\ngraphroot = "{storage_dir}"\n',
        "storage.conf",
    )

    wrapper_path = _install_crun_wrapper(podman_dir, podman_config)
    containers_conf = (
        podman_config.joinpath("containers.conf").read_text().format(crun_gvisor_wrapper_path=wrapper_path)
    )
    write_config(podman_dir / "containers.conf", containers_conf, "containers.conf")
    write_config(
        podman_dir / "registries.conf", podman_config.joinpath("registries.conf").read_text(), "registries.conf"
    )
    write_config(
        podman_dir / "policy.json", podman_config.joinpath("policy.json").read_text(), "policy.json", canary=False
    )

    socket_path = _get_podman_socket_path(settings)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_url = f"unix://{socket_path}"

    containers_env = {
        "CONTAINERS_STORAGE_CONF": str(podman_dir / "storage.conf"),
        "CONTAINERS_CONF": str(podman_dir / "containers.conf"),
        "CONTAINERS_REGISTRIES_CONF": str(podman_dir / "registries.conf"),
        "CONTAINERS_POLICY_PATH": str(podman_dir / "policy.json"),
        # OCI isolation avoids read-only /dev/null from chroot mode's devtmpfs
        "BUILDAH_ISOLATION": "oci",
    }
    return _RuntimeSpec(
        service_name=PODMAN_SERVICE,
        socket_path=socket_path,
        supervisor_command=f"podman system service --time=0 {socket_url}",
        client_env_vars={"DOCKER_HOST": socket_url, **containers_env},
        # Daemon also needs CONTAINERS_* so it finds its isolated config
        daemon_extra_env=containers_env,
        storage_driver=driver,
    )


# ============================================================================
# Unified entry point
# ============================================================================


def get_storage_dir(settings: HookSettings) -> Path | None:
    """Return the shared container storage directory for tmpfs mounting, or None if disabled."""
    if settings.container_runtime == "none":
        return None
    return settings.get_container_storage_dir()


async def setup_container_runtime(
    settings: HookSettings, supervisor: SupervisorClient, tmpfs_mounted: bool
) -> ContainerRuntimeSetup:
    """Set up the configured container runtime (Docker or Podman) under supervisor.

    Idempotent: if the service is already running, skips the start step and
    returns the current state.

    Raises:
        SkipError: If container_runtime is "none".
        PodmanInstallError: If podman installation fails.
    """
    runtime = settings.container_runtime

    if runtime == "podman":
        if shutil.which("podman") is None:
            await install_podman()
        spec = _configure_podman(settings, tmpfs_mounted)
    elif runtime == "docker":
        spec = _configure_docker(settings, tmpfs_mounted)
    else:
        raise SkipError(f"Container runtime disabled (container_runtime={runtime})")

    socket_url = f"unix://{spec.socket_path}"

    if await _is_service_healthy(supervisor, spec.service_name, spec.socket_path):
        logger.info("%s service already running, skipping setup", spec.service_name)
        status = await _snapshot_status(supervisor, spec.service_name)
        return ContainerRuntimeSetup(
            runtime=runtime,
            socket_url=socket_url,
            status=status,
            storage_driver=spec.storage_driver,
            env_vars=spec.client_env_vars,
        )

    # Check for a pre-existing daemon not managed by this supervisor (e.g. the
    # Claude Code web sandbox injects a dockerd before the hook runs).
    if runtime == "docker" and await _is_docker_socket_responsive(spec.socket_path):
        logger.info("Pre-existing dockerd responsive on %s, using it", spec.socket_path)
        return ContainerRuntimeSetup(
            runtime=runtime,
            socket_url=socket_url,
            status="pre-existing",
            storage_driver=spec.storage_driver,
            env_vars=spec.client_env_vars,
        )

    logger.info("Configuring %s...", spec.service_name)

    def on_podman_failure(info: ProcessInfo) -> None:
        log_service_failure("Podman", info)
        podman_dir = settings.get_podman_dir()
        logger.error(
            "Common cause: storage driver mismatch. "
            "If podman was previously used with a different driver, run: rm -rf %s %s",
            podman_dir / "storage",
            podman_dir / "runroot",
        )

    await _start_service(spec, supervisor, on_failure=on_podman_failure if runtime == "podman" else None)

    logger.info("%s service started: DOCKER_HOST=%s", spec.service_name, socket_url)
    status = await _snapshot_status(supervisor, spec.service_name)
    return ContainerRuntimeSetup(
        runtime=runtime,
        socket_url=socket_url,
        status=status,
        storage_driver=spec.storage_driver,
        env_vars=spec.client_env_vars,
    )
