"""Supervisor XML-RPC client with typed wrappers.

Provides typed access to supervisor daemon via XML-RPC API.
"""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import time
import xmlrpc.client
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from supervisor.xmlrpc import Faults

from net_util.net import is_port_in_use
from tools.claude_hooks.errors import ProxyServiceError, SupervisorError

if TYPE_CHECKING:
    from tools.claude_hooks.settings import HookSettings

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


class SupervisorState(BaseModel):
    """Supervisor daemon state from getState()."""

    statecode: int
    statename: str


def is_running(settings: HookSettings) -> bool:
    """Check if supervisord is running."""
    port = settings.get_supervisor_port()
    pidfile = settings.get_supervisor_pidfile()

    # Quick check: port must be listening
    if not is_port_in_use(port):
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
        client = SupervisorClient(settings)
        client.get_state()
        return True
    except (ConnectionError, OSError, xmlrpc.client.Fault) as e:
        logger.debug("Supervisor XML-RPC check failed: %s", e)
        return False


def get_service_config_path(settings: HookSettings, name: str) -> Path:
    """Get the path to a service's config file."""
    return settings.get_supervisor_dir() / "conf.d" / f"{name}.conf"


def read_service_command(settings: HookSettings, name: str) -> str | None:
    """Read the command from a service's config file. Returns None if not found."""
    config_path = get_service_config_path(settings, name)
    if not config_path.exists():
        return None
    config = configparser.ConfigParser()
    config.read(config_path)
    section = f"program:{name}"
    if section not in config:
        return None
    return config[section].get("command")


def write_service_config(
    settings: HookSettings, name: str, command: str, directory: Path, environment: dict[str, str] | None = None
) -> Path:
    """Build and write service config for supervisor."""
    service_conf = get_service_config_path(settings, name)
    service_conf.parent.mkdir(parents=True, exist_ok=True)

    section_content: dict[str, str] = {
        "command": command,
        "directory": str(directory),
        "stdout_logfile": str(settings.get_supervisor_dir() / f"{name}.log"),
        "stderr_logfile": str(settings.get_supervisor_dir() / f"{name}.err.log"),
    }
    if environment:
        # Supervisor environment format: KEY="value",KEY2="value2"
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in environment.items()]
        section_content["environment"] = ",".join(env_parts)

    config = configparser.ConfigParser()
    config[f"program:{name}"] = section_content

    with service_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote service config: %s", service_conf)
    return service_conf


class SupervisorClient:
    """Typed wrapper around supervisor XML-RPC client."""

    def __init__(self, settings: HookSettings) -> None:
        self._settings = settings
        url = self._get_url()
        self._proxy = xmlrpc.client.ServerProxy(url)

    def _get_port(self) -> int:
        return self._settings.get_supervisor_port()

    def _get_url(self) -> str:
        return f"http://127.0.0.1:{self._get_port()}"

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
        if not is_running(self._settings):
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

        write_service_config(self._settings, name, command, directory, environment)

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
        if not is_running(self._settings):
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
        if not is_running(self._settings):
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

    def get_service_command(self, name: str) -> str | None:
        """Get the current command for a service from its config file.

        Note: The supervisor XML-RPC API doesn't expose the command, so we read
        from the config file directly.

        Returns None if the service config doesn't exist.
        """
        return read_service_command(self._settings, name)

    def update_service(
        self, name: str, command: str, directory: Path, environment: dict[str, str] | None = None
    ) -> None:
        """Update an existing service's config and restart it.

        Rewrites the config file, reloads supervisor, removes the old process group,
        and adds the new one. This ensures the new command takes effect.
        """
        if not is_running(self._settings):
            raise SupervisorError(f"supervisord not running, cannot update service {name}")

        write_service_config(self._settings, name, command, directory, environment)

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
