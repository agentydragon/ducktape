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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel
from supervisor.xmlrpc import Faults

from tools.claude_hooks import paths
from tools.claude_hooks.errors import ProxyServiceError, SupervisorError


@dataclass
class SupervisorSetup:
    """Result of supervisor setup."""

    client: SupervisorClient

    @property
    def guidance(self) -> str:
        """Get supervisor usage guidance."""
        supervisor_dir = paths.get_supervisor_dir()
        supervisor_conf = _get_supervisor_conf()
        return textwrap.dedent(
            f"""\
            Supervisor
            ==========
            Supervisor manages background processes (bazel proxy, etc.).
            See: supervisorctl -c {supervisor_conf} status
            Service configs: {supervisor_dir}/conf.d/
            Logs: {supervisor_dir}/
            """
        )

# Default port for supervisor's inet_http_server (localhost only)
_DEFAULT_SUPERVISOR_PORT = 19001

logger = logging.getLogger(__name__)


# Supervisor process state names (see https://supervisord.org/subprocess.html#process-states)
class ProcessState(StrEnum):
    """Supervisor process states."""

    STOPPED = "STOPPED"  # Process stopped or never started
    STARTING = "STARTING"  # Process is starting
    RUNNING = "RUNNING"  # Process is running
    BACKOFF = "BACKOFF"  # Process exited too quickly after starting
    STOPPING = "STOPPING"  # Process is stopping
    EXITED = "EXITED"  # Process exited from RUNNING state
    FATAL = "FATAL"  # Process could not be started
    UNKNOWN = "UNKNOWN"  # Unknown state (programming error)


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


# Default paths (functions to allow testing with env var overrides)
def _get_supervisor_conf() -> Path:
    return paths.get_supervisor_dir() / "supervisord.conf"


def _get_supervisor_port() -> int:
    """Get supervisor TCP port, allowing override via env var."""
    if env_port := os.environ.get("CLAUDE_HOOKS_SUPERVISOR_PORT"):
        return int(env_port)
    return _DEFAULT_SUPERVISOR_PORT


def _get_supervisor_url() -> str:
    """Get supervisor HTTP URL for XML-RPC."""
    return f"http://127.0.0.1:{_get_supervisor_port()}"


def _get_supervisor_log() -> Path:
    return paths.get_supervisor_dir() / "supervisord.log"


def _get_supervisor_pidfile() -> Path:
    return paths.get_supervisor_dir() / "supervisord.pid"


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

    def add_service(self, name: str, command: str, directory: Path, environment: dict[str, str] | None = None) -> None:
        """Add a service to supervisor (idempotent - safe to call multiple times).

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

        # Check if service already exists
        try:
            info = self.get_process_info(name)
            logger.info("Service %s already exists (state=%s)", name, info.statename)
            return
        except xmlrpc.client.Fault as e:
            if e.faultCode != Faults.BAD_NAME:
                # Some other error - not "service doesn't exist"
                raise ProxyServiceError(f"Failed to check service {name}: {e}") from e
            # Service doesn't exist yet, proceed to add it

        _write_service_config(name, command, directory, environment)

        # Reload supervisor config via XML-RPC
        try:
            added, changed, removed = self.reload_config()
            logger.info("Reloaded config: added=%s, changed=%s, removed=%s", added, changed, removed)

            # Retry add_process_group with small delays to handle supervisor timing race
            last_error: xmlrpc.client.Fault | None = None
            for attempt in range(3):
                try:
                    self.add_process_group(name)
                    logger.info("Added and started service: %s", name)
                    # Verify the service was actually registered
                    time.sleep(0.1)  # Brief delay for supervisor to update state
                    try:
                        info = self.get_process_info(name)
                        logger.info("Service %s verified: state=%s", name, info.statename)
                    except xmlrpc.client.Fault as verify_err:
                        logger.warning("Service %s added but not found in verification: %s", name, verify_err)
                    return
                except xmlrpc.client.Fault as e:
                    if e.faultCode == Faults.ALREADY_ADDED:
                        logger.info("Service %s already running", name)
                        return
                    if e.faultCode == Faults.BAD_NAME and attempt < 2:
                        # Supervisor may not be ready yet, retry after small delay
                        time.sleep(0.2)
                        last_error = e
                        continue
                    raise ProxyServiceError(f"Failed to add service {name}: {e}") from e
            if last_error:
                raise ProxyServiceError(f"Failed to add service {name} after retries: {last_error}") from last_error
        except (ConnectionError, OSError) as e:
            raise ProxyServiceError(f"Failed to communicate with supervisor: {e}") from e

    def is_service_running(self, service_name: str, wait_for_start: bool = True, timeout: float = 5.0) -> bool:
        """Check if a specific service is running under supervisor.

        Args:
            service_name: Name of the service to check
            wait_for_start: If True, wait for service to transition from STARTING to RUNNING
            timeout: Maximum time to wait for STARTING->RUNNING transition

        Returns:
            True if service is running, False otherwise
        """
        if not is_running():
            return False

        try:
            deadline = time.time() + timeout if wait_for_start else time.time()
            last_state = None

            while True:
                info = self.get_process_info(service_name)
                if info.statename != last_state:
                    logger.info("Service %s: state=%s", service_name, info.statename)
                    last_state = info.statename
                if info.statename == ProcessState.RUNNING:
                    return True
                if info.statename == ProcessState.STARTING and time.time() < deadline:
                    time.sleep(0.2)
                    continue
                # Service exists but not running (STOPPED, EXITED, FATAL, BACKOFF, etc.)
                return False

        except (ConnectionError, OSError, xmlrpc.client.Fault) as e:
            logger.warning("Service check failed for %s: %s", service_name, e)
            return False

    def restart_service(self, service_name: str) -> None:
        """Restart a specific service under supervisor.

        Raises:
            SupervisorError: If supervisor is not running.
            ProxyServiceError: If service cannot be restarted.
        """
        if not is_running():
            raise SupervisorError("supervisord not running")

        try:
            try:
                self.stop_process(service_name)
            except xmlrpc.client.Fault as e:
                # BAD_NAME means service doesn't exist, NOT_RUNNING means already stopped
                if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
                    raise
            time.sleep(0.3)
            self.start_process(service_name)
            logger.info("Restarted service: %s", service_name)
        except (xmlrpc.client.Fault, ConnectionError, OSError) as e:
            raise ProxyServiceError(f"Failed to restart {service_name}: {e}") from e

    def update_service(self, name: str, command: str, directory: Path, environment: dict[str, str] | None = None) -> None:
        """Update an existing service's config and restart it.

        Rewrites the config file, reloads supervisor, removes the old process group,
        and adds the new one. This ensures the new command takes effect.
        """
        if not is_running():
            raise SupervisorError(f"supervisord not running, cannot update service {name}")

        _write_service_config(name, command, directory, environment)

        # Stop the running process
        try:
            self.stop_process(name)
        except xmlrpc.client.Fault as e:
            if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
                raise

        # Reread config files
        self.reload_config()

        # Remove old process group (unloads old config)
        try:
            self.remove_process_group(name)
        except xmlrpc.client.Fault as e:
            # STILL_RUNNING shouldn't happen after stop, BAD_NAME means not loaded
            if e.faultCode != Faults.BAD_NAME:
                raise

        # Add new process group (loads new config)
        self.add_process_group(name)
        logger.info("Updated service: %s", name)


def _write_config() -> None:
    """Write supervisor configuration file."""
    supervisor_dir = paths.get_supervisor_dir()
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


def _write_service_config(name: str, command: str, directory: Path, environment: dict[str, str] | None = None) -> Path:
    """Build and write service config for supervisor.

    Returns the path to the written config file.
    """
    supervisor_dir = paths.get_supervisor_dir()
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
        client = SupervisorClient()
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


def start() -> SupervisorSetup:
    """Start supervisord if not already running.

    Raises:
        SupervisorError: If supervisor cannot be started.
    """
    if is_running():
        logger.info("supervisord already running")
        return SupervisorSetup(client=SupervisorClient())

    logger.info("Starting supervisord...")

    # Clean up any stale files from previous crashed supervisor
    _cleanup_stale_supervisor_files()

    supervisor_conf = _get_supervisor_conf()

    # Ensure config exists
    if not supervisor_conf.exists():
        _write_config()

    # Validate config file is readable
    if not supervisor_conf.is_file():
        raise SupervisorError(f"Config file not found or not a file: {supervisor_conf}")
    try:
        config_parser = configparser.ConfigParser()
        config_parser.read(supervisor_conf)
        if not config_parser.has_section("supervisord"):
            raise SupervisorError(f"Invalid config: missing [supervisord] section in {supervisor_conf}")
    except Exception as e:
        raise SupervisorError(f"Invalid config file {supervisor_conf}: {e}") from e

    # Start supervisord using Python module to ensure it's on the right Python path
    # Use Popen with start_new_session to fully detach the daemon process
    # Log stderr to supervisor log for debugging startup failures
    supervisor_log = _get_supervisor_log()
    supervisor_dir = paths.get_supervisor_dir()
    supervisor_port = _get_supervisor_port()

    # Log what we're about to execute
    cmd = [sys.executable, "-m", "supervisor.supervisord", "-c", str(supervisor_conf)]
    logger.info("Starting supervisor with command: %s", " ".join(cmd))
    logger.info("  config: %s", supervisor_conf)
    logger.info("  log: %s", supervisor_log)
    logger.info("  port: %d", supervisor_port)
    logger.info("  dir: %s", supervisor_dir)

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=supervisor_dir,
        )
        # Give it a tiny bit to check if it crashes immediately
        time.sleep(0.1)
        returncode = process.poll()
        if returncode is not None:
            # Process exited immediately - read log for details
            log_content = supervisor_log.read_text() if supervisor_log.exists() else "(log not found)"
            raise SupervisorError(
                f"supervisord exited immediately with code {returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Log: {log_content[-1000:]}"
            )
        logger.info("supervisord process spawned (pid=%s)", process.pid)
    except OSError as e:
        raise SupervisorError(f"Failed to spawn supervisord: {e}") from e

    # Wait for supervisor to be ready (up to 5 seconds)
    for i in range(20):
        time.sleep(0.25)
        if is_running():
            logger.info("supervisord started successfully")
            return SupervisorSetup(client=SupervisorClient())
        if i % 4 == 3:  # Log every second
            logger.debug("Waiting for supervisord... (%d/20)", i + 1)

    # Gather comprehensive debug info for the error
    debug_info = _dump_supervisor_debug_info()

    raise SupervisorError(f"supervisord did not start in time\n{debug_info}")



