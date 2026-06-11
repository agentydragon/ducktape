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
import textwrap
from dataclasses import dataclass, field
from importlib.resources.abc import Traversable
from pathlib import Path

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import ProcessInfo, ProcessState, SupervisorClient

logger = logging.getLogger(__name__)

PODMAN_SERVICE = "podman"


class PodmanInstallError(Exception):
    """Raised when podman installation fails."""


class PodmanConfigConflictError(Exception):
    """Raised when existing config file conflicts with what we want to write."""


def _write_config_conservative(path: Path, content: str, description: str) -> None:
    """Write config file conservatively - only if no conflict.

    Accepts:
    - File doesn't exist: create it
    - File exists with exact same content: no-op (idempotent)

    Rejects:
    - File exists with different content: raises PodmanConfigConflictError
    """
    if path.exists():
        existing = path.read_text()
        if existing == content:
            logger.debug("Config %s already has expected content", path)
            return
        raise PodmanConfigConflictError(
            f"Existing {description} at {path} has unexpected content. "
            f"Expected our gVisor-compatible config but found different content. "
            f"Delete the file to allow reconfiguration: rm {path}"
        )
    path.write_text(content)
    logger.debug("Wrote %s to %s", description, path)


@dataclass
class PodmanSetup:
    """Result of podman setup.

    Status and guidance are snapshotted at setup time.
    """

    socket_url: str
    status: str
    guidance: str
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

    # Install podman
    process = await asyncio.create_subprocess_exec(
        "apt-get", "install", "-y", "podman", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    if process.returncode != 0:
        raise PodmanInstallError(f"apt-get install podman failed: {stderr.decode()}")
    logger.info("Podman installed successfully")

    # Verify installation
    if not is_podman_available():
        raise PodmanInstallError("podman not found after installation")


def setup_podman_storage(settings: HookSettings) -> dict[str, str]:
    """Configure podman for gVisor compatibility with isolated paths.

    Uses isolated configuration to avoid conflicts with system podman:
    - Config files: ~/.cache/claude-hooks/podman/
    - Storage: ~/.cache/claude-hooks/podman/storage/
    - policy.json: ~/.config/containers/policy.json (user-level, hardcoded lookup path)

    gVisor sandbox restrictions require:
    1. VFS storage driver (no overlay filesystem support)
    2. Host user namespace (userns = "host")
    3. run.oci.keep_original_groups=1 annotation

    Uses conservative file writing - only writes if file doesn't exist or
    already has the exact content we want to write.

    Returns:
        Dict of environment variables to export (CONTAINERS_CONF, etc.)

    Raises:
        PodmanConfigConflictError: If existing config file has conflicting content.
    """
    podman_dir = settings.get_podman_dir()
    podman_dir.mkdir(parents=True, exist_ok=True)

    podman_config: Traversable = importlib.resources.files("tools.claude_hooks.config.podman")

    # Storage paths (isolated from system podman)
    storage_dir = podman_dir / "storage"
    runroot_dir = podman_dir / "runroot"
    storage_dir.mkdir(parents=True, exist_ok=True)
    runroot_dir.mkdir(parents=True, exist_ok=True)

    # Generate storage.conf with custom paths
    storage_conf_path = podman_dir / "storage.conf"
    storage_conf_content = textwrap.dedent(f"""\
        [storage]
        driver = "vfs"
        runroot = "{runroot_dir}"
        graphroot = "{storage_dir}"
    """)
    _write_config_conservative(storage_conf_path, storage_conf_content, "storage.conf")

    # Container runtime configuration
    containers_conf_path = podman_dir / "containers.conf"
    containers_conf_content = podman_config.joinpath("containers.conf").read_text()
    _write_config_conservative(containers_conf_path, containers_conf_content, "containers.conf")

    # Registry configuration (allows short image names like "alpine")
    registries_conf_path = podman_dir / "registries.conf"
    registries_conf_content = podman_config.joinpath("registries.conf").read_text()
    _write_config_conservative(registries_conf_path, registries_conf_content, "registries.conf")

    # Policy.json goes to user-level config dir (hardcoded lookup path in podman)
    # ~/.config/containers/policy.json is checked before /etc/containers/policy.json
    containers_config_dir = settings.get_containers_config_dir()
    containers_config_dir.mkdir(parents=True, exist_ok=True)
    policy_json_path = containers_config_dir / "policy.json"
    policy_json_content = podman_config.joinpath("policy.json").read_text()
    _write_config_conservative(policy_json_path, policy_json_content, "policy.json")

    logger.info("Configured podman for gVisor: VFS storage at %s", storage_dir)

    # Return env vars for podman to use our isolated config
    return _get_podman_env_vars(settings)


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


async def _snapshot_podman_guidance(
    supervisor: SupervisorClient, settings: HookSettings, socket_url: str
) -> tuple[str, str]:
    """Snapshot podman status and guidance. Returns (status, guidance)."""
    try:
        info = await supervisor.get_process_info(PODMAN_SERVICE)
        status: str = info.statename
    except Exception:
        status = ProcessState.UNKNOWN

    podman_dir = settings.get_podman_dir()
    guidance = textwrap.dedent(
        f"""\
        Podman in gVisor Sandbox
        ========================
        Podman is configured with gVisor-specific workarounds.
        Running under supervisor (state: {status}). DOCKER_HOST={socket_url}

        Use fully qualified image names (docker.io/library/...)

        Configuration Applied:
        ----------------------
        - VFS storage driver (gVisor has no overlay fs)
        - Isolated config: {podman_dir}
        - userns = "host"
        - run.oci.keep_original_groups=1 annotation (auto-applied)
        """
    )
    return status, guidance


async def setup_podman(settings: HookSettings, supervisor: SupervisorClient) -> PodmanSetup:
    """Set up podman storage and start service.

    If podman is not installed, attempts to install it via apt.
    Idempotent: if podman service is already running, returns immediately.

    Returns:
        PodmanSetup with socket URL, snapshotted status/guidance, and env vars

    Raises:
        SkipError: If skip_podman is True in settings.
        PodmanInstallError: If podman installation fails.
    """
    if settings.skip_podman:
        logger.info("Skipping podman setup (skip_podman=True)")
        raise SkipError("Podman")

    socket_path = _get_socket_path(settings)
    socket_url = f"unix://{socket_path}"

    # Check if podman service is already running (idempotent case)
    if await _is_podman_service_healthy(supervisor, socket_path):
        logger.info("Podman service already running, skipping setup")
        env_vars = _get_podman_env_vars(settings)
        status, guidance = await _snapshot_podman_guidance(supervisor, settings, socket_url)
        return PodmanSetup(socket_url=socket_url, status=status, guidance=guidance, env_vars=env_vars)

    if not is_podman_available():
        logger.info("Podman not found, installing...")
        await install_podman()

    logger.info("Configuring podman...")
    env_vars = setup_podman_storage(settings)
    socket_url, service_env = await start_podman_service(settings, supervisor, env_vars)
    env_vars.update(service_env)
    logger.info("Podman service started: DOCKER_HOST=%s", socket_url)
    status, guidance = await _snapshot_podman_guidance(supervisor, settings, socket_url)
    return PodmanSetup(socket_url=socket_url, status=status, guidance=guidance, env_vars=env_vars)


def _get_podman_env_vars(settings: HookSettings) -> dict[str, str]:
    """Get podman env vars for already-configured setup.

    Includes container config paths and any proxy/SSL env vars from the
    current environment. The podman daemon runs under supervisor which
    doesn't inherit env vars implicitly — they must be passed explicitly.
    Without proxy vars, the daemon can't pull images through a TLS proxy.
    """
    podman_dir = settings.get_podman_dir()
    env_vars: dict[str, str] = {
        "CONTAINERS_STORAGE_CONF": str(podman_dir / "storage.conf"),
        "CONTAINERS_CONF": str(podman_dir / "containers.conf"),
        "CONTAINERS_REGISTRIES_CONF": str(podman_dir / "registries.conf"),
    }
    # Pass proxy and SSL CA env vars so the daemon can pull images through
    # the TLS-inspecting proxy (e.g., Anthropic's egress proxy)
    for var in PROXY_ENV_VARS + SSL_CA_ENV_VARS:
        if value := os.environ.get(var):
            env_vars[var] = value
    return env_vars


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

    # Start podman system service (--time=0 means never timeout, keep running)
    # Pass config env vars so podman uses our isolated paths
    await supervisor.add_service(
        name=PODMAN_SERVICE,
        command=f"podman system service --time=0 {socket_url}",
        directory=Path.home(),
        environment=env_vars,
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
