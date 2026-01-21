"""Podman system service management.

Starts podman system service under supervisor to provide Docker-compatible API.
Uses isolated configuration to avoid conflicts with system podman.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from importlib.resources.abc import Traversable
from pathlib import Path

from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.paths import get_containers_config_dir, get_podman_dir
from tools.claude_hooks.supervisor_setup import ProcessState, SupervisorClient

logger = logging.getLogger(__name__)

PODMAN_SERVICE = "podman"
SKIP_ENV_VAR = "CLAUDE_HOOKS_SKIP_PODMAN"


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

    Args:
        path: Path to write to
        content: Content to write
        description: Human-readable description for error messages
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
    """Result of podman setup."""

    socket_url: str
    supervisor: SupervisorClient
    env_vars: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Get human-readable podman status."""
        if self.supervisor.is_service_running(PODMAN_SERVICE, wait_for_start=False):
            return "running"
        return "not running"

    @property
    def guidance(self) -> str:
        """Get podman usage guidance for gVisor sandbox."""
        podman_dir = get_podman_dir()
        return textwrap.dedent(
            f"""\
            Podman in gVisor Sandbox
            ========================
            Podman is configured with gVisor-specific workarounds.
            Running under supervisor (status: {self.status}). DOCKER_HOST={self.socket_url}

            Use fully qualified image names (docker.io/library/...)

            Configuration Applied:
            ----------------------
            - VFS storage driver (gVisor has no overlay fs)
            - Isolated config: {podman_dir}
            - userns = "host"
            - run.oci.keep_original_groups=1 annotation (auto-applied)
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


def setup_podman_storage() -> dict[str, str]:
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
    podman_dir = get_podman_dir()
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
    containers_config_dir = get_containers_config_dir()
    containers_config_dir.mkdir(parents=True, exist_ok=True)
    policy_json_path = containers_config_dir / "policy.json"
    policy_json_content = podman_config.joinpath("policy.json").read_text()
    _write_config_conservative(policy_json_path, policy_json_content, "policy.json")

    logger.info("Configured podman for gVisor: VFS storage at %s", storage_dir)

    # Return env vars for podman to use our isolated config
    return _get_podman_env_vars()


def _get_socket_path() -> Path:
    """Get podman socket path (in isolated directory)."""
    return get_podman_dir() / "podman.sock"


def setup_podman(supervisor: SupervisorClient) -> PodmanSetup:
    """Set up podman storage and start service.

    If podman is not installed, attempts to install it via apt.
    Idempotent: if podman service is already running, returns immediately.

    Args:
        supervisor: Supervisor client for managing services

    Returns:
        PodmanSetup with socket URL, supervisor client, and env vars to export

    Raises:
        SkipError: If CLAUDE_HOOKS_SKIP_PODMAN is set.
        PodmanInstallError: If podman installation fails.
    """
    if os.environ.get(SKIP_ENV_VAR):
        logger.info("Skipping podman setup (%s set)", SKIP_ENV_VAR)
        raise SkipError("Podman", SKIP_ENV_VAR)

    socket_path = _get_socket_path()
    socket_url = f"unix://{socket_path}"

    # Check if podman service is already running (idempotent case)
    if _is_podman_service_healthy(supervisor, socket_path):
        logger.info("Podman service already running, skipping setup")
        # Still need to return env vars for the session
        env_vars = _get_podman_env_vars()
        return PodmanSetup(socket_url=socket_url, supervisor=supervisor, env_vars=env_vars)

    if not is_podman_available():
        logger.info("Podman not found, installing...")
        install_podman()

    logger.info("Configuring podman...")
    env_vars = setup_podman_storage()
    socket_url, service_env = start_podman_service(supervisor, env_vars)
    env_vars.update(service_env)
    logger.info(f"Podman service started: DOCKER_HOST={socket_url}")
    return PodmanSetup(socket_url=socket_url, supervisor=supervisor, env_vars=env_vars)


def _get_podman_env_vars() -> dict[str, str]:
    """Get podman env vars for already-configured setup."""
    podman_dir = get_podman_dir()
    return {
        "CONTAINERS_STORAGE_CONF": str(podman_dir / "storage.conf"),
        "CONTAINERS_CONF": str(podman_dir / "containers.conf"),
        "CONTAINERS_REGISTRIES_CONF": str(podman_dir / "registries.conf"),
    }


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


def start_podman_service(supervisor: SupervisorClient, env_vars: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Start podman system service under supervisor.

    Args:
        supervisor: Supervisor client for adding services
        env_vars: Environment variables for podman config paths

    Provides Docker-compatible API at Unix socket.
    Does NOT start infrastructure containers (PostgreSQL, Registry, Proxy).

    Returns:
        Tuple of (socket_url, additional_env_vars including DOCKER_HOST)

    Raises:
        TimeoutError: If socket doesn't become ready in time
    """
    logger.info("Starting podman system service...")

    socket_path = _get_socket_path()
    socket_url = f"unix://{socket_path}"
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # Start podman system service (--time=0 means never timeout, keep running)
    # Pass config env vars so podman uses our isolated paths
    supervisor.add_service(
        name="podman",
        command=f"podman system service --time=0 {socket_url}",
        directory=Path.home(),
        environment=env_vars,
    )

    # Wait for socket to be ready
    _wait_for_socket(socket_path, supervisor, timeout=10)

    logger.info("Podman service ready at %s", socket_url)
    return socket_url, {"DOCKER_HOST": socket_url}


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
    podman_dir = get_podman_dir()
    hint = (
        "Common cause: storage driver mismatch. "
        f"If podman was previously used with a different driver, run: "
        f"rm -rf {podman_dir / 'storage'} {podman_dir / 'runroot'}"
    )
    raise TimeoutError(f"Podman socket {socket_path} did not become ready in {timeout}s ({diag}). {hint}")
