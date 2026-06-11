"""Supervisor startup and management for Claude Code web.

Provides functions to start and manage the supervisord daemon.

Uses TCP socket (inet_http_server) instead of Unix socket to avoid 9p filesystem
limitations in gVisor sandbox where hard linking Unix sockets fails with EOPNOTSUPP.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import sys
import textwrap
from dataclasses import dataclass

from net_util.net import is_port_in_use
from tools.claude_hooks.errors import SupervisorError
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient, try_connect

logger = logging.getLogger(__name__)


@dataclass
class SupervisorSetup:
    """Result of supervisor setup."""

    client: SupervisorClient
    settings: HookSettings

    @property
    def guidance(self) -> str:
        """Get supervisor usage guidance."""
        supervisor_dir = self.settings.get_supervisor_dir()
        supervisor_conf = supervisor_dir / "supervisord.conf"
        python_exe = sys.executable
        return textwrap.dedent(
            f"""\
            Supervisor
            ==========
            Supervisor manages background processes (auth proxy, etc.).
            See: {python_exe} -m supervisor.supervisorctl -c {supervisor_conf} status
            Service configs: {supervisor_dir}/conf.d/
            Logs: {supervisor_dir}/
            """
        )


def _write_config(settings: HookSettings) -> None:
    """Write supervisor configuration file."""
    supervisor_dir = settings.get_supervisor_dir()
    supervisor_dir.mkdir(parents=True, exist_ok=True)

    supervisor_conf = supervisor_dir / "supervisord.conf"
    supervisor_port = settings.get_supervisor_port()
    supervisor_url = f"http://127.0.0.1:{supervisor_port}"
    supervisor_log = supervisor_dir / "supervisord.log"
    supervisor_pidfile = settings.get_supervisor_pidfile()

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


def _cleanup_stale_supervisor_files(settings: HookSettings) -> None:
    """Clean up stale supervisor pidfile.

    Called before starting supervisord when try_connect() returns None
    but the pidfile still exists (stale state).
    """
    pidfile = settings.get_supervisor_pidfile()
    if pidfile.exists():
        logger.info("Removing stale supervisor pidfile: %s", pidfile)
        pidfile.unlink()


def _dump_supervisor_debug_info(settings: HookSettings) -> str:
    """Gather comprehensive debug info for supervisor startup failures."""
    lines = []
    supervisor_dir = settings.get_supervisor_dir()
    supervisor_log = supervisor_dir / "supervisord.log"
    port = settings.get_supervisor_port()
    pidfile = settings.get_supervisor_pidfile()

    # State of key files
    lines.append("=== Supervisor state ===")
    lines.append(f"Port {port} listening: {is_port_in_use(port)}")
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


async def start(settings: HookSettings) -> SupervisorSetup:
    """Start supervisord if not already running.

    Raises:
        SupervisorError: If supervisor cannot be started.
    """
    existing_client = await try_connect(settings)
    if existing_client:
        logger.info("supervisord already running")
        return SupervisorSetup(client=existing_client, settings=settings)

    logger.info("Starting supervisord...")

    # Clean up any stale files from previous crashed supervisor
    _cleanup_stale_supervisor_files(settings)

    supervisor_dir = settings.get_supervisor_dir()
    supervisor_conf = supervisor_dir / "supervisord.conf"

    # Ensure config exists
    if not supervisor_conf.exists():
        _write_config(settings)

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
    supervisor_log = supervisor_dir / "supervisord.log"
    supervisor_port = settings.get_supervisor_port()

    # Log what we're about to execute
    cmd = [sys.executable, "-m", "supervisor.supervisord", "-c", str(supervisor_conf)]
    logger.info("Starting supervisor with command: %s", " ".join(cmd))
    logger.info("  config: %s", supervisor_conf)
    logger.info("  log: %s", supervisor_log)
    logger.info("  port: %d", supervisor_port)
    logger.info("  dir: %s", supervisor_dir)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=supervisor_dir,
        )
        # Give it a tiny bit to check if it crashes immediately
        await asyncio.sleep(0.1)
        if process.returncode is not None and process.returncode != 0:
            # Process exited with error - read log for details
            # Note: exit code 0 is SUCCESS (daemon forked and parent exited normally)
            log_content = supervisor_log.read_text() if supervisor_log.exists() else "(log not found)"
            raise SupervisorError(
                f"supervisord exited immediately with code {process.returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Log: {log_content[-1000:]}"
            )
        logger.info("supervisord process spawned (pid=%s)", process.pid)
    except OSError as e:
        raise SupervisorError(f"Failed to spawn supervisord: {e}") from e

    # Wait for supervisor to be ready (up to 5 seconds)
    for i in range(20):
        await asyncio.sleep(0.25)
        client = await try_connect(settings)
        if client:
            logger.info("supervisord started successfully")
            return SupervisorSetup(client=client, settings=settings)
        if i % 4 == 3:  # Log every second
            logger.debug("Waiting for supervisord... (%d/20)", i + 1)

    # Gather comprehensive debug info for the error
    debug_info = _dump_supervisor_debug_info(settings)

    raise SupervisorError(f"supervisord did not start in time\n{debug_info}")
