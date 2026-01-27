"""Supervisor cleanup and assertion utilities for testing."""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Generator
from pathlib import Path

from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import try_connect


async def supervisor_is_running(settings: HookSettings) -> bool:
    """Check if supervisord is running (test helper)."""
    return await try_connect(settings) is not None


def stop_supervisor_by_pidfile(pidfile: Path) -> None:
    """Stop supervisor process by reading and killing from pidfile."""
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except (ValueError, ProcessLookupError, OSError):
        pass
    with contextlib.suppress(OSError):
        pidfile.unlink()


@contextlib.contextmanager
def supervisor_cleanup(pidfile: Path) -> Generator[None]:
    """Context manager for supervisor cleanup before and after test."""
    stop_supervisor_by_pidfile(pidfile)
    try:
        yield
    finally:
        stop_supervisor_by_pidfile(pidfile)
