"""E2E tests for daemon lifecycle management.

Tests the full _ensure_daemon flow: locking, liveness detection via flock,
killing hung daemons, and starting fresh ones. Uses the real hook daemon
subprocess (double-forked), exercising the actual pidfile flock and UDS
health endpoint.
"""

import os
import signal
import threading
import time

import pytest_bazel

from devinfra.claude.hook_daemon.client import _ensure_daemon, _kill_daemon_by_pidfile, check_health, read_pidfile
from devinfra.claude.session_paths import SessionPaths


def _wait_for_pid_death(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"Process {pid} still alive after {timeout}s")


def test_ensure_daemon_starts_fresh(daemon_paths: SessionPaths) -> None:
    """_ensure_daemon starts a daemon when none is running."""
    _ensure_daemon(daemon_paths)
    assert check_health(daemon_paths.hook_daemon_sock)


def test_ensure_daemon_noop_when_healthy(daemon_paths: SessionPaths) -> None:
    """_ensure_daemon returns immediately if a healthy daemon exists."""
    _ensure_daemon(daemon_paths)
    original_pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    _ensure_daemon(daemon_paths)

    assert read_pidfile(daemon_paths.hook_daemon_pidfile) == original_pid
    assert check_health(daemon_paths.hook_daemon_sock)


def test_concurrent_ensure_daemon(daemon_paths: SessionPaths) -> None:
    """Two concurrent _ensure_daemon calls don't race to start two daemons."""
    _ensure_daemon(daemon_paths)
    original_pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    results: list[Exception | None] = [None, None]

    def run_ensure(idx: int) -> None:
        try:
            _ensure_daemon(daemon_paths)
        except Exception as e:
            results[idx] = e

    t1 = threading.Thread(target=run_ensure, args=(0,))
    t2 = threading.Thread(target=run_ensure, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert results[0] is None, f"Thread 0 failed: {results[0]}"
    assert results[1] is None, f"Thread 1 failed: {results[1]}"
    assert read_pidfile(daemon_paths.hook_daemon_pidfile) == original_pid


def test_kill_daemon_by_pidfile(daemon_paths: SessionPaths) -> None:
    """_kill_daemon_by_pidfile terminates a daemon holding the pidfile flock."""
    _ensure_daemon(daemon_paths)
    pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    _kill_daemon_by_pidfile(daemon_paths.hook_daemon_pidfile)

    _wait_for_pid_death(pid)


def test_ensure_daemon_kills_hung_daemon(daemon_paths: SessionPaths) -> None:
    """_ensure_daemon kills a hung daemon (flock held, health check fails) and starts a new one."""
    _ensure_daemon(daemon_paths)
    old_pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    # Simulate a hung daemon: remove the socket so health checks fail, but keep
    # the process alive (flock still held).  This is equivalent to a daemon that
    # is alive but unresponsive — _ensure_daemon should detect the flock and kill it.
    daemon_paths.hook_daemon_sock.unlink()
    assert not check_health(daemon_paths.hook_daemon_sock)

    _ensure_daemon(daemon_paths)

    assert check_health(daemon_paths.hook_daemon_sock)
    new_pid = read_pidfile(daemon_paths.hook_daemon_pidfile)
    assert new_pid != old_pid

    _wait_for_pid_death(old_pid)


def test_ensure_daemon_replaces_dead_daemon(daemon_paths: SessionPaths) -> None:
    """_ensure_daemon cleans up after a crashed daemon (flock released, stale socket)."""
    _ensure_daemon(daemon_paths)
    pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    # SIGKILL simulates crash/OOM — kernel releases flock, stale socket may remain.
    os.kill(pid, signal.SIGKILL)
    _wait_for_pid_death(pid)
    assert daemon_paths.hook_daemon_pidfile.exists()

    _ensure_daemon(daemon_paths)
    assert check_health(daemon_paths.hook_daemon_sock)


if __name__ == "__main__":
    pytest_bazel.main()
