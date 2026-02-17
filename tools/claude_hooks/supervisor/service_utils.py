"""Shared utilities for supervisor-managed container services (Docker, Podman)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from tools.claude_hooks.supervisor.client import ProcessInfo, ProcessState, SupervisorClient

logger = logging.getLogger(__name__)


async def wait_for_service_socket(
    supervisor: SupervisorClient, service_name: str, socket_path: Path, on_failure: Callable[[ProcessInfo], None]
) -> None:
    """Wait for supervisor service socket to be created and service to be running.

    Generic helper for Docker, Podman, and other socket-based supervisor services.

    Caller should wrap with asyncio.timeout() to set deadline.

    Args:
        supervisor: Supervisor client
        service_name: Name of the supervisor service (e.g., "dockerd", "podman")
        socket_path: Path to the Unix socket
        on_failure: Callback to log failure details (called before raising)

    Raises:
        TimeoutError: If service enters a terminal failure state
    """
    while True:
        info = await supervisor.get_process_info(service_name)

        if socket_path.exists() and info.statename == ProcessState.RUNNING:
            return

        # Terminal failure states — no point waiting
        if info.statename in (ProcessState.FATAL, ProcessState.BACKOFF, ProcessState.EXITED):
            on_failure(info)
            raise TimeoutError(
                f"{service_name} service entered {info.statename} (socket_exists={socket_path.exists()}). "
                f"Check logs for details."
            )

        await asyncio.sleep(0.1)


def log_service_failure(service_name: str, info: ProcessInfo) -> None:
    """Log diagnostic info for a failed supervisor service.

    Args:
        service_name: Human-readable service name for log messages
        info: ProcessInfo from supervisor
    """
    logger.error("%s service failed: %s", service_name, info.model_dump())

    for stream, logfile in (("stdout", info.stdout_logfile), ("stderr", info.stderr_logfile)):
        if logfile:
            logpath = Path(logfile)
            if logpath.exists():
                content = logpath.read_text()
                if content.strip():
                    logger.error("%s %s:\n%s", service_name, stream, content)
