"""Async supervisor XML-RPC client with typed wrappers.

Provides typed async access to supervisor daemon via XML-RPC API,
using httpx for native asyncio support. Uses Connection: close to
handle supervisor's HTTP/1.0 XML-RPC server.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import xmlrpc.client
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic import BaseModel
from supervisor.xmlrpc import Faults

from net_util.net import is_port_in_use
from tools.claude_hooks.errors import ProxyServiceError

if TYPE_CHECKING:
    from tools.claude_hooks.settings import HookSettings

logger = logging.getLogger(__name__)

# Timeout for XML-RPC calls to supervisor (seconds)
XMLRPC_TIMEOUT = 30


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


async def _xmlrpc_call(url: str, method: str, params: tuple[Any, ...], request_timeout: float) -> Any:
    """Make an async XML-RPC call using httpx.

    Supervisor's XML-RPC server uses HTTP/1.0, so we use Connection: close
    to avoid httpx expecting keep-alive behavior. Proxy is disabled since
    supervisor always runs on localhost.
    """
    body = xmlrpc.client.dumps(params, method)
    async with httpx.AsyncClient(timeout=request_timeout, trust_env=False) as client:
        response = await client.post(url, content=body, headers={"Content-Type": "text/xml", "Connection": "close"})
    response.raise_for_status()
    result, _ = xmlrpc.client.loads(response.content)
    return result[0] if len(result) == 1 else result


async def try_connect(settings: HookSettings) -> SupervisorClient | None:
    """Try to connect to a running supervisord.

    Returns a connected client if supervisor is reachable, None otherwise.
    Performs quick pre-checks (port, pidfile) before attempting XML-RPC.
    """
    port = settings.get_supervisor_port()
    pidfile = settings.get_supervisor_pidfile()

    logger.debug("try_connect check: port=%d, pidfile=%s", port, pidfile)

    # Quick check: port must be listening
    if not is_port_in_use(port):
        logger.info("Supervisor port %d not listening", port)
        return None
    logger.debug("Port %d is in use", port)

    # Check pidfile and if process is alive
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            # Check if process exists (signal 0 doesn't kill, just checks)
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            logger.debug("Supervisor pidfile exists but process not running")
            return None

    try:
        client = SupervisorClient(settings)
        await client.get_state()
        return client
    except (ConnectionError, OSError, xmlrpc.client.Fault, httpx.ConnectError) as e:
        logger.debug("Supervisor XML-RPC check failed: %s", e)
        return None


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
        # Must use double quotes — supervisor doesn't accept single quotes (shlex.quote).
        env_parts = [f'{k}="{v}"' for k, v in environment.items()]
        section_content["environment"] = ",".join(env_parts)

    config = configparser.ConfigParser()
    config[f"program:{name}"] = section_content

    with service_conf.open("w") as f:
        config.write(f)
    logger.info("Wrote service config: %s", service_conf)
    return service_conf


class SupervisorClient:
    """Async typed wrapper around supervisor XML-RPC API.

    Uses httpx with Connection: close for compatibility with
    supervisor's HTTP/1.0 XML-RPC server.
    """

    def __init__(self, settings: HookSettings, timeout: float = XMLRPC_TIMEOUT) -> None:
        self._settings = settings
        self._url = f"http://127.0.0.1:{settings.get_supervisor_port()}/RPC2"
        self._timeout = timeout
        logger.info("SupervisorClient connecting to %s (timeout=%ds)", self._url, timeout)

    async def _call(self, method: str, *params: Any) -> Any:
        """Make an XML-RPC call to supervisor."""
        return await _xmlrpc_call(self._url, method, params, self._timeout)

    async def get_state(self) -> SupervisorState:
        """Get supervisor daemon state."""
        return SupervisorState.model_validate(await self._call("supervisor.getState"))

    async def get_process_info(self, name: str) -> ProcessInfo:
        """Get info for a specific process."""
        return ProcessInfo.model_validate(await self._call("supervisor.getProcessInfo", name))

    async def get_all_process_info(self) -> list[ProcessInfo]:
        """Get info for all processes."""
        all_info = cast(list[dict[str, object]], await self._call("supervisor.getAllProcessInfo"))
        return [ProcessInfo.model_validate(info) for info in all_info]

    async def service_exists(self, name: str) -> bool:
        """Check if a service is registered with supervisor."""
        return any(p.name == name for p in await self.get_all_process_info())

    async def reload_config(self) -> tuple[list[str], list[str], list[str]]:
        """Reload config files. Returns (added, changed, removed) process names."""
        # reloadConfig returns [[added, changed, removed]]
        result = cast(list[list[list[str]]], await self._call("supervisor.reloadConfig"))
        added, changed, removed = result[0]
        return (added, changed, removed)

    async def add_process_group(self, name: str) -> bool:
        """Add a process group. Returns True on success."""
        return bool(await self._call("supervisor.addProcessGroup", name))

    async def remove_process_group(self, name: str) -> bool:
        """Remove a process group. Returns True on success."""
        return bool(await self._call("supervisor.removeProcessGroup", name))

    async def start_process(self, name: str, wait: bool = True) -> bool:
        """Start a process. Returns True on success."""
        return bool(await self._call("supervisor.startProcess", name, wait))

    async def stop_process(self, name: str, wait: bool = True) -> bool:
        """Stop a process. Returns True on success."""
        return bool(await self._call("supervisor.stopProcess", name, wait))

    async def add_service(
        self, name: str, command: str, directory: Path, environment: dict[str, str] | None = None
    ) -> None:
        """Add a service to supervisor (idempotent - safe to call multiple times).

        Raises:
            ProxyServiceError: If service cannot be added.
        """
        # Check if service already exists
        if await self.service_exists(name):
            info = await self.get_process_info(name)
            logger.info("Service %s already exists (state=%s)", name, info.statename)
            return

        write_service_config(self._settings, name, command, directory, environment)

        # Reload supervisor config via XML-RPC
        try:
            added, changed, removed = await self.reload_config()
            logger.info("Reloaded config: added=%s, changed=%s, removed=%s", added, changed, removed)

            # Retry add_process_group with small delays to handle supervisor timing race
            last_error: xmlrpc.client.Fault | None = None
            for attempt in range(3):
                try:
                    await self.add_process_group(name)
                    logger.info("Added and started service: %s", name)
                    # Brief delay for supervisor to update state
                    await asyncio.sleep(0.1)
                    try:
                        info = await self.get_process_info(name)
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
                        await asyncio.sleep(0.2)
                        last_error = e
                        continue
                    raise ProxyServiceError(f"Failed to add service {name}: {e}") from e
            if last_error:
                raise ProxyServiceError(f"Failed to add service {name} after retries: {last_error}") from last_error
        except (ConnectionError, OSError) as e:
            raise ProxyServiceError(f"Failed to communicate with supervisor: {e}") from e

    async def get_service_state(self, service_name: str) -> ProcessState | None:
        """Get the current state of a service.

        Returns None if service doesn't exist.
        """
        if not await self.service_exists(service_name):
            return None
        return (await self.get_process_info(service_name)).statename

    async def is_service_running(self, service_name: str) -> bool:
        """Check if a specific service is currently running."""
        return await self.get_service_state(service_name) == ProcessState.RUNNING

    async def wait_for_service_running(self, service_name: str, *, interval: float = 0.25) -> None:
        """Poll until a service reports RUNNING.

        Callers should wrap with asyncio.timeout() to enforce a deadline.
        """
        running = await self.is_service_running(service_name)
        while not running:
            await asyncio.sleep(interval)
            running = await self.is_service_running(service_name)

    async def restart_service(self, service_name: str) -> None:
        """Restart a specific service under supervisor.

        Raises:
            ProxyServiceError: If service cannot be restarted.
        """
        try:
            try:
                await self.stop_process(service_name)
            except xmlrpc.client.Fault as e:
                # BAD_NAME means service doesn't exist, NOT_RUNNING means already stopped
                if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
                    raise
            await asyncio.sleep(0.3)
            await self.start_process(service_name)
            logger.info("Restarted service: %s", service_name)
        except (xmlrpc.client.Fault, ConnectionError, OSError) as e:
            raise ProxyServiceError(f"Failed to restart {service_name}: {e}") from e

    def get_service_command(self, name: str) -> str | None:
        """Get the current command for a service from its config file.

        Note: The supervisor XML-RPC API doesn't expose the command, so we read
        from the config file directly. This is a sync operation (filesystem only).
        """
        return read_service_command(self._settings, name)

    async def update_service(
        self, name: str, command: str, directory: Path, environment: dict[str, str] | None = None
    ) -> None:
        """Update an existing service's config and restart it.

        Rewrites the config file, reloads supervisor, removes the old process group,
        and adds the new one. This ensures the new command takes effect.
        """
        write_service_config(self._settings, name, command, directory, environment)

        # Stop the running process
        try:
            await self.stop_process(name)
        except xmlrpc.client.Fault as e:
            if e.faultCode not in (Faults.BAD_NAME, Faults.NOT_RUNNING):
                raise

        # Reread config files
        await self.reload_config()

        # Remove old process group (unloads old config)
        try:
            await self.remove_process_group(name)
        except xmlrpc.client.Fault as e:
            # STILL_RUNNING shouldn't happen after stop, BAD_NAME means not loaded
            if e.faultCode != Faults.BAD_NAME:
                raise

        # Add new process group (loads new config)
        await self.add_process_group(name)
        logger.info("Updated service: %s", name)
