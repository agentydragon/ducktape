"""Supervisor cleanup utilities for testing."""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Generator
from pathlib import Path


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
