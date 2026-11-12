"""Shared test utilities to avoid duplication across test files."""

import os
import subprocess
from collections.abc import Callable
from datetime import timedelta

from python.runfiles import runfiles
from tenacity import RetryError, Retrying, stop_after_delay, wait_fixed

_RUNFILES = runfiles.Create()


def _get_wt_cli_path() -> str:
    """Get path to wt_cli binary via Bazel runfiles."""
    if _RUNFILES is None:
        raise RuntimeError("Runfiles not available - must run under Bazel")
    path = _RUNFILES.Rlocation("ducktape/x/wt/wt_cli")
    if path is None:
        raise RuntimeError("wt_cli binary not found in runfiles")
    return path


def run_cli_command(args, cwd=None, env=None, timeout: timedelta = timedelta(seconds=60.0)):
    """Run the actual CLI command as subprocess."""
    cmd = [_get_wt_cli_path(), *args]
    if env is None:
        env = os.environ.copy()
    seconds = timeout.total_seconds()
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=seconds, check=False)


def wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 5.0, interval_seconds: float = 0.1) -> bool:
    """Poll `predicate` until it returns True or timeout elapses.

    Returns True if the condition became true within the timeout; False otherwise.
    """

    def _check() -> bool:
        result = predicate()
        if not result:
            raise RuntimeError("predicate not yet true")
        return result

    try:
        Retrying(stop=stop_after_delay(timeout_seconds), wait=wait_fixed(interval_seconds), reraise=True)(_check)
        return True
    except RetryError, RuntimeError:
        # Timeout or predicate never became true
        return False
