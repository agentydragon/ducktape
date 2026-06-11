"""Supervisor setup for managing long-running processes in Claude Code web.

Provides a centralized process manager for:
- Bazel proxy (handles TLS-inspecting proxy authentication)
- Future: other background services as needed

Configuration via environment variables (for testing):
- CLAUDE_HOOKS_SUPERVISOR_DIR: Override supervisor directory
"""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import subprocess
import sys
import textwrap
import time
import xmlrpc.client
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel
from supervisor.xmlrpc import Faults, SupervisorTransport

from claude_hooks.errors import ProxyServiceError, SupervisorError

logger = logging.getLogger(__name__)

# Supervisor process state names (see https://supervisord.org/subprocess.html#process-states)
ProcessState = Literal["STOPPED", "STARTING", "RUNNING", "BACKOFF", "STOPPING", "EXITED", "FATAL", "UNKNOWN"]


class ProcessInfo(BaseModel):
    """Supervisor process info from getProcessInfo/getAllProcessInfo."""

    name: str
    group: str
    start: int
    stop: int
    now: int
    state: int
    statename: ProcessState
    spawnerr: str
    exitstatus: int
    logfile: str  # deprecated, alias for stdout_logfile
    stdout_logfile: str
    stderr_logfile: str
    pid: int
    description: str


def _get_supervisor_dir() -> Path:
    """Get supervisor directory, allowing override via env var."""
    if env_dir := os.environ.get("CLAUDE_HOOKS_SUPERVISOR_DIR"):
        return Path(env_dir)
    return Path.home() / ".config" / "supervisor"


# Default paths (functions to allow testing with env var overrides)
def _get_supervisor_conf() -> Path:
    return _get_supervisor_dir() / "supervisord.conf"


def _get_supervisor_sock() -> Path:
    return _get_supervisor_dir() / "supervisor.sock"


def _get_supervisor_log() -> Path:
    return _get_supervisor_dir() / "supervisord.log"


def _get_supervisor_pidfile() -> Path:
    return _get_supervisor_dir() / "supervisord.pid"


class SupervisorState(BaseModel):
    """Supervisor daemon state from getState()."""

    statecode: int
    statename: str


class SupervisorClient:
    """Typed wrapper around supervisor XML-RPC client."""

    def __init__(self) -> None:
        sock = _get_supervisor_sock()
        if not sock.exists():
            raise ConnectionError(f"Supervisor socket not found: {sock}")
        transport = SupervisorTransport(None, None, f"unix://{sock}")
        self._proxy = xmlrpc.client.ServerProxy("http://127.0.0.1", transport=transport)

    def get_state(self) -> SupervisorState:
        """Get supervisor daemon state."""
        return SupervisorState.model_validate(self._proxy.supervisor.getState())

    def get_process_info(self, name: str) -> ProcessInfo:
        """Get info for a specific process."""
        return ProcessInfo.model_validate(self._proxy.supervisor.getProcessInfo(name))

    def get_all_process_info(self) -> list[ProcessInfo]:
        """Get info for all processes."""
        all_info = cast(list[dict[str, object]], self._proxy.supervisor.getAllProcessInfo())
        return [ProcessInfo.model_validate(info) for info in all_info]

    def reload_config(self) -> tuple[list[str], list[str], list[str]]:
        """Reload config files. Returns (added, changed, removed) process names."""
        result = cast(list[list[list[str]]], self._proxy.supervisor.reloadConfig())
        added, changed, removed = result[0]
        return (added, changed, removed)

    def add_process_group(self, name: str) -> bool:
        """Add a process group. Returns True on success."""
        return bool(self._proxy.supervisor.addProcessGroup(name))

    def remove_process_group(self, name: str) -> bool:
        """Remove a process group. Returns True on success."""
        return bool(self._proxy.supervisor.removeProcessGroup(name))

    def start_process(self, name: str, wait: bool = True) -> bool:
        """Start a process. Returns True on success."""
        return bool(self._proxy.supervisor.startProcess(name, wait))

    def stop_process(self, name: str, wait: bool = True) -> bool:
        """Stop a process. Returns True on success."""
        return bool(self._proxy.supervisor.stopProcess(name, wait))


def _get_supervisor_client() -> SupervisorClient:
    """Get typed XML-RPC client for supervisor."""
    return SupervisorClient()


def _write_config() -> None:
    """Write supervisor configuration file."""
    supervisor_dir = _get_supervisor_dir()
    supervisor_conf = _get_supervisor_conf()
    supervisor_sock = _get_supervisor_sock()
    supervisor_log = _get_supervisor_log()
    supervisor_pidfile = _get_supervisor_pidfile()

    supervisor_dir.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    config["unix_http_server"] = {"file": str(supervisor_sock)}
    config["supervisord"] = {
        "logfile": str(supervisor_log),
        "pidfile": str(supervisor_pidfile),
        "childlogdir": str(supervisor_dir),
        "nodaemon": "false",
        "silent": "false",
    }
    config["rpcinterface:supervisor"] = {
        "supervisor.rpcinterface_factory": "supervisor.rpcinterface:make_main_rpcinterface"
    }
    config["supervisorctl"] = {"serverurl": f"unix://{supervisor_sock}"}
    config["include"] = {"files": f"{supervisor_dir}/conf.d/*.conf"}

    with supervisor_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote supervisor config to %s", supervisor_conf)

    # Create conf.d directory for service configs
    (supervisor_dir / "conf.d").mkdir(parents=True, exist_ok=True)


def _build_service_config(
    name: str, command: str, directory: Path, environment: dict[str, str] | None = None
) -> configparser.ConfigParser:
    """Build service config for supervisor."""
    supervisor_dir = _get_supervisor_dir()
    config = configparser.ConfigParser()
    section = f"program:{name}"
    config[section] = {
        "command": command,
        "directory": str(directory),
        "stdout_logfile": str(supervisor_dir / f"{name}.log"),
        "stderr_logfile": str(supervisor_dir / f"{name}.err.log"),
    }

    if environment:
        # Supervisor environment format: KEY="value",KEY2="value2"
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in environment.items()]
        config[section]["environment"] = ",".join(env_parts)

    return config


def is_running() -> bool:
    """Check if supervisord is running."""
    if not _get_supervisor_sock().exists():
        return False

    try:
        client = _get_supervisor_client()
        client.get_state()
        return True
    except (ConnectionError, OSError, xmlrpc.client.Fault) as e:
        logger.warning("Supervisor check failed: %s", e)
        return False


def start() -> None:
    """Start supervisord if not already running.

    Raises:
        SupervisorError: If supervisor cannot be started.
    """
    if is_running():
        logger.info("supervisord already running")
        return

    logger.info("Starting supervisord...")

    supervisor_conf = _get_supervisor_conf()

    # Ensure config exists
    if not supervisor_conf.exists():
        _write_config()

    # Start supervisord using Python module to ensure it's on the right Python path
    try:
        result = subprocess.run(
            [sys.executable, "-m", "supervisor.supervisord", "-c", supervisor_conf],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise SupervisorError(f"Failed to start supervisord: {result.stderr}")
    except subprocess.TimeoutExpired as e:
        raise SupervisorError("supervisord startup timed out") from e

    # Wait for supervisor to be ready
    for _ in range(10):
        time.sleep(0.3)
        if is_running():
            logger.info("supervisord started successfully")
            return

    raise SupervisorError("supervisord did not start in time")


def add_service(name: str, command: str, directory: Path, environment: dict[str, str] | None = None) -> None:
    """Add a service to supervisor.

    Args:
        name: Service name (used in supervisorctl commands)
        command: Command to run
        directory: Working directory
        environment: Environment variables (optional)

    Raises:
        SupervisorError: If supervisor is not running.
        ProxyServiceError: If service cannot be added.
    """
    if not is_running():
        raise SupervisorError(f"supervisord not running, cannot add service {name}")

    service_conf = _get_supervisor_dir() / "conf.d" / f"{name}.conf"
    config = _build_service_config(name, command, directory, environment)

    with service_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote service config: %s", service_conf)

    # Reload supervisor config via XML-RPC
    try:
        client = _get_supervisor_client()
        added, changed, removed = client.reload_config()
        logger.info("Reloaded config: added=%s, changed=%s, removed=%s", added, changed, removed)

        client.add_process_group(name)
        logger.info("Added and started service: %s", name)
    except xmlrpc.client.Fault as e:
        # addProcessGroup raises ALREADY_ADDED if service exists, which is fine
        if e.faultCode == Faults.ALREADY_ADDED:
            logger.info("Service %s already running", name)
            return
        raise ProxyServiceError(f"Failed to add service {name}: {e}") from e
    except (ConnectionError, OSError) as e:
        raise ProxyServiceError(f"Failed to communicate with supervisor: {e}") from e


def is_service_running(service_name: str) -> bool:
    """Check if a specific service is running under supervisor.

    Args:
        service_name: Name of the service to check

    Returns:
        True if service is running, False otherwise
    """
    if not is_running():
        return False

    try:
        client = _get_supervisor_client()
        info = client.get_process_info(service_name)
        return info.statename == "RUNNING"
    except (ConnectionError, OSError, xmlrpc.client.Fault) as e:
        logger.warning("Service check failed for %s: %s", service_name, e)
        return False


def restart_service(service_name: str) -> None:
    """Restart a specific service under supervisor.

    Raises:
        SupervisorError: If supervisor is not running.
        ProxyServiceError: If service cannot be restarted.
    """
    if not is_running():
        raise SupervisorError("supervisord not running")

    try:
        client = _get_supervisor_client()
        try:
            client.stop_process(service_name)
        except xmlrpc.client.Fault as e:
            # BAD_NAME means service doesn't exist, NOT_RUNNING means already stopped
            if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
                raise
        time.sleep(0.3)
        client.start_process(service_name)
        logger.info("Restarted service: %s", service_name)
    except (xmlrpc.client.Fault, ConnectionError, OSError) as e:
        raise ProxyServiceError(f"Failed to restart {service_name}: {e}") from e


def update_service(name: str, command: str, directory: Path, environment: dict[str, str] | None = None) -> None:
    """Update an existing service's config and restart it.

    Rewrites the config file, reloads supervisor, removes the old process group,
    and adds the new one. This ensures the new command takes effect.
    """
    if not is_running():
        raise SupervisorError(f"supervisord not running, cannot update service {name}")

    service_conf = _get_supervisor_dir() / "conf.d" / f"{name}.conf"
    config = _build_service_config(name, command, directory, environment)

    with service_conf.open("w") as f:
        config.write(f)
    logger.info("Updated service config: %s", service_conf)

    client = _get_supervisor_client()

    # Stop the running process
    try:
        client.stop_process(name)
    except xmlrpc.client.Fault as e:
        if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
            raise

    # Reread config files
    client.reload_config()

    # Remove old process group (unloads old config)
    try:
        client.remove_process_group(name)
    except xmlrpc.client.Fault as e:
        # STILL_RUNNING shouldn't happen after stop, BAD_NAME means not loaded
        if e.faultCode != Faults.BAD_NAME:
            raise

    # Add new process group (loads new config and starts)
    client.add_process_group(name)
    logger.info("Updated and restarted service: %s", name)


def get_status() -> str:
    """Get human-readable supervisor status."""
    if not is_running():
        return "not running"

    try:
        client = _get_supervisor_client()
        all_info = client.get_all_process_info()
        running = sum(1 for info in all_info if info.statename == "RUNNING")
        return f"running ({running} services)"
    except (ConnectionError, OSError, xmlrpc.client.Fault):
        return "error"


def emit_usage_guidance() -> None:
    """Emit supervisor usage guidance (visible to agent)."""
    supervisor_dir = _get_supervisor_dir()
    supervisor_conf = _get_supervisor_conf()
    guidance = textwrap.dedent(
        f"""\
        Supervisor
        ==========
        Supervisor manages background processes (bazel proxy, etc.).
        See: supervisorctl -c {supervisor_conf} status
        Service configs: {supervisor_dir}/conf.d/
        Logs: {supervisor_dir}/
        """
    )
    print(guidance)
