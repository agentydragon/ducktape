"""Podman system service management.

Starts podman system service under supervisor to provide Docker-compatible API.
Uses isolated configuration to avoid conflicts with system podman.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from importlib.resources.abc import Traversable
from pathlib import Path

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.managed_files import write_config
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import ProcessInfo, ProcessState, SupervisorClient

logger = logging.getLogger(__name__)

PODMAN_SERVICE = "podman"


class PodmanInstallError(Exception):
    """Raised when podman installation fails."""


@dataclass
class PodmanSetup:
    """Result of podman setup."""

    socket_url: str
    status: str
    storage_driver: str = "vfs"
    env_vars: dict[str, str] = field(default_factory=dict)


def is_podman_available() -> bool:
    """Check if podman binary is available in PATH."""
    return shutil.which("podman") is not None


async def install_podman() -> None:
    """Install podman via apt if not already installed.

    Raises:
        FileNotFoundError: If apt-get is not available.
        TimeoutError: If apt operations time out.
        PodmanInstallError: If installation fails for other reasons.
    """
    if is_podman_available():
        logger.info("Podman already installed")
        return

    logger.info("Installing podman via apt...")

    # Update apt cache (non-fatal if it fails)
    process = await asyncio.create_subprocess_exec(
        "apt-get", "update", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    if process.returncode != 0:
        logger.warning("apt-get update failed: %s", stderr.decode())

    # Install podman and crun (crun is needed by the crun-gvisor-wrapper)
    process = await asyncio.create_subprocess_exec(
        "apt-get", "install", "-y", "podman", "crun", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    if process.returncode != 0:
        raise PodmanInstallError(f"apt-get install podman crun failed: {stderr.decode()}")
    logger.info("Podman and crun installed successfully")

    # Verify installation
    if not is_podman_available():
        raise PodmanInstallError("podman not found after installation")


def _install_crun_wrapper(podman_dir: Path, podman_config: Traversable) -> Path:
    """Install crun-gvisor-wrapper script to podman directory.

    Returns the installed wrapper path for use in containers.conf.
    """
    wrapper_path = podman_dir / "crun-gvisor-wrapper"
    wrapper_source = podman_config.joinpath("crun_gvisor_wrapper.py").read_text()
    write_config(wrapper_path, wrapper_source, "crun-gvisor-wrapper")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper_path


def _render_containers_conf(podman_config: Traversable, wrapper_path: Path) -> str:
    """Render containers.conf with the crun-gvisor-wrapper as the default runtime.

    The template uses {crun_gvisor_wrapper_path} as a placeholder for the
    installed wrapper path.
    """
    template = podman_config.joinpath("containers.conf").read_text()
    return template.format(crun_gvisor_wrapper_path=wrapper_path)


def setup_podman_storage(settings: HookSettings, tmpfs_root: Path | None) -> tuple[dict[str, str], str]:
    """Configure podman for gVisor compatibility with isolated paths.

    Uses isolated configuration to avoid conflicts with system podman:
    - Config files: ~/.cache/claude-hooks/podman/
    - Storage: overlay on tmpfs (preferred) or VFS on 9p (fallback)
    - policy.json: ~/.config/containers/policy.json (user-level, hardcoded lookup path)

    gVisor sandbox restrictions require:
    1. Overlay on tmpfs (preferred): tmpfs supports xattr, enabling native
       overlay with layer caching. Layer limit: ~50 layers per build
       (kernel mount option page size). Builds exceeding this must use
       ``--layers=false``. Falls back to VFS on 9p if tmpfs is unavailable.
    2. Host user namespace (userns = "host")
    3. run.oci.keep_original_groups=1 annotation

    Uses conservative file writing - only writes if file doesn't exist or
    already has the exact content we want to write. Files with the canary
    marker are preserved; files without are overwritten.

    Args:
        settings: Hook settings.
        tmpfs_root: Path to exec-capable tmpfs mount. If provided, overlay
            storage is placed here for faster I/O and layer caching.
    """
    podman_dir = settings.get_podman_dir()
    podman_dir.mkdir(parents=True, exist_ok=True)

    podman_config: Traversable = importlib.resources.files("tools.claude_hooks.config.podman")

    # Choose storage driver and root based on tmpfs availability
    if tmpfs_root is not None:
        storage_root = tmpfs_root / "podman-overlay"
        driver = "overlay"
    else:
        storage_root = podman_dir
        driver = "vfs"
    storage_dir = storage_root / "storage"
    runroot_dir = storage_root / "run"
    storage_dir.mkdir(parents=True, exist_ok=True)
    runroot_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Using %s storage at %s", driver, storage_dir)

    # Generate storage.conf (TOML format — podman requires quoted string values)
    storage_conf_path = podman_dir / "storage.conf"
    storage_conf_content = f'[storage]\ndriver = "{driver}"\nrunroot = "{runroot_dir}"\ngraphroot = "{storage_dir}"\n'
    write_config(storage_conf_path, storage_conf_content, "storage.conf")

    # Install crun-gvisor-wrapper (injects keep_original_groups annotation for buildah)
    wrapper_path = _install_crun_wrapper(podman_dir, podman_config)

    # Container runtime configuration (uses wrapper as default runtime)
    containers_conf_path = podman_dir / "containers.conf"
    containers_conf_content = _render_containers_conf(podman_config, wrapper_path)
    write_config(containers_conf_path, containers_conf_content, "containers.conf")

    # Registry configuration (allows short image names like "alpine")
    registries_conf_path = podman_dir / "registries.conf"
    registries_conf_content = podman_config.joinpath("registries.conf").read_text()
    write_config(registries_conf_path, registries_conf_content, "registries.conf")

    # Policy.json goes to user-level config dir (hardcoded lookup path in podman)
    # ~/.config/containers/policy.json is checked before /etc/containers/policy.json
    containers_config_dir = settings.get_containers_config_dir()
    containers_config_dir.mkdir(parents=True, exist_ok=True)
    policy_json_path = containers_config_dir / "policy.json"
    policy_json_content = podman_config.joinpath("policy.json").read_text()
    write_config(policy_json_path, policy_json_content, "policy.json", canary=False)

    # Return env vars for podman to use our isolated config, and the driver used
    return _get_podman_env_vars(settings), driver


def _get_socket_path(settings: HookSettings) -> Path:
    """Get podman socket path.

    Unix sockets have a 108-character path limit (UNIX_PATH_MAX). When XDG_CACHE_HOME
    is set to a deeply nested path (e.g., in Bazel test environments), the socket path
    can exceed this limit. We use a shorter path in /tmp with a hash for uniqueness.
    """
    if settings.podman_socket is not None:
        return settings.podman_socket

    # Use a hash of the podman dir to create a unique but short socket path
    podman_dir = settings.get_podman_dir()
    dir_hash = hashlib.sha256(str(podman_dir).encode()).hexdigest()[:12]
    return Path(f"/tmp/claude-podman-{dir_hash}.sock")


async def _snapshot_podman_status(supervisor: SupervisorClient) -> str:
    """Snapshot podman supervisor process status."""
    try:
        info = await supervisor.get_process_info(PODMAN_SERVICE)
        return info.statename
    except Exception:
        return ProcessState.UNKNOWN


async def setup_podman(settings: HookSettings, supervisor: SupervisorClient, tmpfs_root: Path | None) -> PodmanSetup:
    """Set up podman storage and start service.

    If podman is not installed, attempts to install it via apt.
    Idempotent: if podman service is already running, returns immediately.

    Args:
        settings: Hook settings.
        supervisor: Supervisor client for process management.
        tmpfs_root: Path to exec-capable tmpfs. If provided, podman uses
            overlay storage on tmpfs instead of VFS on 9p.

    Raises:
        SkipError: If install_podman is False in settings.
        PodmanInstallError: If podman installation fails.
    """
    if not settings.install_podman:
        logger.info("Skipping podman setup (install_podman=False)")
        raise SkipError("Podman")

    socket_path = _get_socket_path(settings)
    socket_url = f"unix://{socket_path}"

    # Check if podman service is already running (idempotent case)
    if await _is_podman_service_healthy(supervisor, socket_path):
        logger.info("Podman service already running, skipping setup")
        env_vars = _get_podman_env_vars(settings)
        status = await _snapshot_podman_status(supervisor)
        return PodmanSetup(socket_url=socket_url, status=status, env_vars=env_vars)

    if not is_podman_available():
        logger.info("Podman not found, installing...")
        await install_podman()

    logger.info("Configuring podman...")
    env_vars, storage_driver = setup_podman_storage(settings, tmpfs_root=tmpfs_root)
    socket_url, service_env = await start_podman_service(settings, supervisor, env_vars)
    env_vars.update(service_env)
    logger.info("Podman service started: DOCKER_HOST=%s", socket_url)
    status = await _snapshot_podman_status(supervisor)
    return PodmanSetup(socket_url=socket_url, status=status, storage_driver=storage_driver, env_vars=env_vars)


def _get_podman_env_vars(settings: HookSettings) -> dict[str, str]:
    """Get podman config env vars for already-configured setup.

    Returns only podman-specific config paths (CONTAINERS_*, BUILDAH_*).
    Proxy/SSL env vars are NOT included here — they're merged separately
    in start_podman_service() for the daemon, and the session env file's
    auth proxy section sets SSL CA vars to the correct combined CA bundle.
    """
    podman_dir = settings.get_podman_dir()
    return {
        "CONTAINERS_STORAGE_CONF": str(podman_dir / "storage.conf"),
        "CONTAINERS_CONF": str(podman_dir / "containers.conf"),
        "CONTAINERS_REGISTRIES_CONF": str(podman_dir / "registries.conf"),
        # OCI isolation avoids read-only /dev/null from chroot mode's devtmpfs
        "BUILDAH_ISOLATION": "oci",
    }


async def _is_podman_service_healthy(supervisor: SupervisorClient, socket_path: Path) -> bool:
    """Check if podman service is running and socket exists.

    Used for idempotency: skip setup if service is already healthy.
    """
    if not socket_path.exists():
        return False
    try:
        return await supervisor.is_service_running(PODMAN_SERVICE)
    except Exception:
        return False


async def start_podman_service(
    settings: HookSettings, supervisor: SupervisorClient, env_vars: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Start podman system service under supervisor.

    Provides Docker-compatible API at Unix socket.
    Does NOT start infrastructure containers (PostgreSQL, Registry, Proxy).

    Returns:
        Tuple of (socket_url, additional_env_vars including DOCKER_HOST)

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    logger.info("Starting podman system service...")

    socket_path = _get_socket_path(settings)
    socket_url = f"unix://{socket_path}"
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # The podman daemon runs under supervisor which doesn't inherit the
    # container's env vars. Merge proxy/SSL vars from the current environment
    # so the daemon can pull images through the TLS-inspecting egress proxy.
    # These are NOT included in _get_podman_env_vars() because that dict is
    # also exported to the session env file, where the auth proxy section
    # already sets SSL CA vars to the correct combined CA bundle.
    daemon_env = dict(env_vars)
    for var in PROXY_ENV_VARS + SSL_CA_ENV_VARS:
        if value := os.environ.get(var):
            daemon_env[var] = value

    # Start podman system service (--time=0 means never timeout, keep running)
    # Pass config env vars so podman uses our isolated paths
    await supervisor.add_service(
        name=PODMAN_SERVICE,
        command=f"podman system service --time=0 {socket_url}",
        directory=Path.home(),
        environment=daemon_env,
    )

    # Wait for socket to be ready
    async with asyncio.timeout(10):
        await _wait_for_socket(settings, socket_path, supervisor)

    logger.info("Podman service ready at %s", socket_url)
    return socket_url, {"DOCKER_HOST": socket_url}


async def _wait_for_socket(settings: HookSettings, socket_path: Path, supervisor: SupervisorClient) -> None:
    """Wait for Unix socket to be created and service to be running.

    Caller should wrap with asyncio.timeout() to set deadline.

    Raises:
        PodmanServiceError: If service enters a terminal failure state.
    """
    while True:
        info = await supervisor.get_process_info(PODMAN_SERVICE)

        if socket_path.exists() and info.statename == ProcessState.RUNNING:
            return

        # Terminal failure states — no point waiting
        if info.statename in (ProcessState.FATAL, ProcessState.BACKOFF, ProcessState.EXITED):
            _log_podman_failure(info)
            podman_dir = settings.get_podman_dir()
            hint = (
                "Common cause: storage driver mismatch. "
                f"If podman was previously used with a different driver, run: "
                f"rm -rf {podman_dir / 'storage'} {podman_dir / 'runroot'}"
            )
            raise TimeoutError(
                f"Podman service entered {info.statename} (socket_exists={socket_path.exists()}). {hint}"
            )

        await asyncio.sleep(0.1)


def _log_podman_failure(info: ProcessInfo) -> None:
    """Log diagnostic info for a failed podman service."""
    logger.error("Podman service failed: %s", info.model_dump())
    for logfile_attr in ("stdout_logfile", "stderr_logfile"):
        logfile = getattr(info, logfile_attr, None)
        if logfile:
            logpath = Path(logfile)
            if logpath.exists():
                content = logpath.read_text()
                if content.strip():
                    logger.error("Podman %s:\n%s", logfile_attr, content)
