"""Supervisor setup for managing long-running processes in Claude Code web.

Provides a centralized process manager for:
- Bazel proxy (handles TLS-inspecting proxy authentication)
- Future: other background services as needed

Configuration via environment variables (for testing):
- CLAUDE_HOOKS_SUPERVISOR_DIR: Override supervisor directory
- CLAUDE_HOOKS_SUPERVISOR_PORT: Override supervisor TCP port (default: 19001)

Uses TCP socket (inet_http_server) instead of Unix socket to avoid 9p filesystem
limitations in gVisor sandbox where hard linking Unix sockets fails with EOPNOTSUPP.
"""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import socket
import subprocess
import sys
import textwrap
import time
import xmlrpc.client
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel
from supervisor.xmlrpc import Faults

from tools.claude_hooks.errors import ProxyServiceError, SupervisorError

# Default port for supervisor's inet_http_server (localhost only)
_DEFAULT_SUPERVISOR_PORT = 19001

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


def _get_supervisor_port() -> int:
    """Get supervisor TCP port, allowing override via env var."""
    if env_port := os.environ.get("CLAUDE_HOOKS_SUPERVISOR_PORT"):
        return int(env_port)
    return _DEFAULT_SUPERVISOR_PORT


def _get_supervisor_url() -> str:
    """Get supervisor HTTP URL for XML-RPC."""
    return f"http://127.0.0.1:{_get_supervisor_port()}"


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
        url = _get_supervisor_url()
        self._proxy = xmlrpc.client.ServerProxy(url)

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
    supervisor_port = _get_supervisor_port()
    supervisor_url = _get_supervisor_url()
    supervisor_log = _get_supervisor_log()
    supervisor_pidfile = _get_supervisor_pidfile()

    supervisor_dir.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    # Use TCP socket instead of Unix socket to avoid 9p filesystem limitations
    # in gVisor sandbox (hard linking Unix sockets fails with EOPNOTSUPP)
    config["inet_http_server"] = {"port": f"127.0.0.1:{supervisor_port}"}
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
    config["supervisorctl"] = {"serverurl": supervisor_url}
    config["include"] = {"files": f"{supervisor_dir}/conf.d/*.conf"}

    with supervisor_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote supervisor config to %s", supervisor_conf)

    # Create conf.d directory for service configs
    (supervisor_dir / "conf.d").mkdir(parents=True, exist_ok=True)


def _write_service_config(
    name: str, command: str, directory: Path, environment: dict[str, str] | None = None
) -> Path:
    """Build and write service config for supervisor.

    Returns the path to the written config file.
    """
    supervisor_dir = _get_supervisor_dir()
    service_conf = supervisor_dir / "conf.d" / f"{name}.conf"
    service_conf.parent.mkdir(parents=True, exist_ok=True)

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

    with service_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote service config: %s", service_conf)
    return service_conf


def _is_port_in_use(port: int) -> bool:
    """Check if a TCP port is in use on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_running() -> bool:
    """Check if supervisord is running."""
    port = _get_supervisor_port()
    pidfile = _get_supervisor_pidfile()

    # Quick check: port must be listening
    if not _is_port_in_use(port):
        logger.debug("Supervisor port %d not listening", port)
        return False

    # Check pidfile and if process is alive
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            # Check if process exists (signal 0 doesn't kill, just checks)
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            logger.debug("Supervisor pidfile exists but process not running")
            return False

    try:
        client = _get_supervisor_client()
        client.get_state()
        return True
    except (ConnectionError, OSError, xmlrpc.client.Fault) as e:
        logger.debug("Supervisor XML-RPC check failed: %s", e)
        return False


def _cleanup_stale_supervisor_files() -> None:
    """Clean up stale supervisor pidfile.

    Called before starting supervisord when is_running() returns False
    but the pidfile still exists (stale state).
    """
    pidfile = _get_supervisor_pidfile()

    if pidfile.exists():
        logger.info("Removing stale supervisor pidfile: %s", pidfile)
        pidfile.unlink()


def _dump_supervisor_debug_info() -> str:
    """Gather comprehensive debug info for supervisor startup failures."""
    lines = []
    supervisor_log = _get_supervisor_log()
    port = _get_supervisor_port()
    pidfile = _get_supervisor_pidfile()

    # State of key files
    lines.append("=== Supervisor state ===")
    lines.append(f"Port {port} listening: {_is_port_in_use(port)}")
    lines.append(f"Pidfile exists: {pidfile.exists()}")

    if pidfile.exists():
        try:
            pid_content = pidfile.read_text().strip()
            lines.append(f"Pidfile content: {pid_content}")
            pid = int(pid_content)
            # Check if process exists
            try:
                os.kill(pid, 0)
                lines.append(f"Process {pid}: exists")
            except ProcessLookupError:
                lines.append(f"Process {pid}: not found")
            except PermissionError:
                lines.append(f"Process {pid}: exists (permission denied)")
        except (ValueError, OSError) as e:
            lines.append(f"Pidfile read error: {e}")

    # Full log content (limited to last 4KB to avoid massive output)
    if supervisor_log.exists():
        log_content = supervisor_log.read_text()
        if len(log_content) > 4096:
            log_content = f"... (truncated, showing last 4KB) ...\n{log_content[-4096:]}"
        lines.append("=== supervisord.log ===")
        lines.append(log_content)
    else:
        lines.append("=== supervisord.log: does not exist ===")

    return "\n".join(lines)


def start() -> None:
    """Start supervisord if not already running.

    Raises:
        SupervisorError: If supervisor cannot be started.
    """
    if is_running():
        logger.info("supervisord already running")
        return

    logger.info("Starting supervisord...")

    # Clean up any stale files from previous crashed supervisor
    _cleanup_stale_supervisor_files()

    supervisor_conf = _get_supervisor_conf()

    # Ensure config exists
    if not supervisor_conf.exists():
        _write_config()

    # Start supervisord using Python module to ensure it's on the right Python path
    # Use Popen with start_new_session to fully detach the daemon process
    # Log stderr to supervisor log for debugging startup failures
    supervisor_log = _get_supervisor_log()
    with supervisor_log.open("a") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "supervisor.supervisord", "-c", supervisor_conf],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for supervisor to be ready (up to 5 seconds)
    for i in range(20):
        time.sleep(0.25)
        if is_running():
            logger.info("supervisord started successfully")
            return
        if i % 4 == 3:  # Log every second
            logger.debug("Waiting for supervisord... (%d/20)", i + 1)

    # Gather comprehensive debug info for the error
    debug_info = _dump_supervisor_debug_info()

    raise SupervisorError(f"supervisord did not start in time\n{debug_info}")


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

    _write_service_config(name, command, directory, environment)

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

    _write_service_config(name, command, directory, environment)

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
