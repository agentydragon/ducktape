"""Shared test utilities to avoid duplication across test files."""

from datetime import timedelta
import os
import subprocess
import time
from typing import Callable


def add_project_root_to_env(env: dict) -> None:
    """Deprecated: no-op. Rely on installed wt package for imports."""
    return


def run_cli_command(args, cwd=None, env=None, timeout: timedelta = timedelta(seconds=60.0), stdin=None):
    """Run the actual CLI command as subprocess."""
    cmd = ["python3", "-m", "wt.cli", *args]
    if env is None:
        env = os.environ.copy()
    add_project_root_to_env(env)
    seconds = timeout.total_seconds()
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=seconds, check=False, stdin=stdin
    )


def run_cli_sh_command(args, env, timeout: timedelta = timedelta(seconds=60.0)):
    """Run the CLI command with 'sh' subcommand as subprocess."""
    return run_cli_command(list(args), env=env, timeout=timeout)


def wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 5.0, interval_seconds: float = 0.1) -> bool:
    """Poll `predicate` until it returns True or timeout elapses.

    Returns True if the condition became true within the timeout; False otherwise.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            # Treat exceptions as transient until timeout; tests can inspect state after
            pass
        time.sleep(interval_seconds)
    return False
