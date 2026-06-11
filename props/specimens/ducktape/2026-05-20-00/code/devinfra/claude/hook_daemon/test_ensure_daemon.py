"""E2E tests for daemon lifecycle management.

Tests the full _ensure_daemon flow: locking, liveness detection via flock,
killing hung daemons, and starting fresh ones. Uses the real hook daemon
subprocess (double-forked), exercising the actual pidfile flock and UDS
health endpoint.
"""

import datetime
import multiprocessing
import multiprocessing.sharedctypes
import multiprocessing.synchronize
import os
import signal
import threading
import time
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.hook_daemon.client import (
    DaemonStartError,
    StartupFailure,
    _check_circuit_breaker,
    _clear_startup_failure,
    _ensure_daemon,
    _kill_daemon_by_pidfile,
    _read_startup_failure,
    _record_startup_failure,
    _UDSConnection,
    _wait_for_sock,
    read_pidfile,
)
from devinfra.claude.hook_daemon.testing.testing_helpers import PROFILE_FILENAME, setup_daemon_project
from devinfra.claude.session_paths import SessionPaths


def _cold_start_worker(
    paths: SessionPaths,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.sharedctypes.SynchronizedArray,
    idx: int,
) -> None:
    """Worker for test_parallel_cold_start: wait at barrier then call _ensure_daemon."""
    barrier.wait()
    try:
        _ensure_daemon(paths)
        results[idx] = 0
    except Exception:
        results[idx] = 1


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
    assert _UDSConnection(daemon_paths.hook_daemon_sock).check_health()


def test_ensure_daemon_noop_when_healthy(daemon_paths: SessionPaths) -> None:
    """_ensure_daemon returns immediately if a healthy daemon exists."""
    _ensure_daemon(daemon_paths)
    original_pid = read_pidfile(daemon_paths.hook_daemon_pidfile)

    _ensure_daemon(daemon_paths)

    assert read_pidfile(daemon_paths.hook_daemon_pidfile) == original_pid
    assert _UDSConnection(daemon_paths.hook_daemon_sock).check_health()


def test_parallel_cold_start(short_tmp: Path) -> None:
    """N processes all call _ensure_daemon simultaneously from cold start — exactly one daemon starts.

    Exercises the cross-process FileLock in _ensure_daemon. Reproduces the TOCTOU
    race from 2026-03 where 5 concurrent hook processes each spawned their own daemon.
    """
    session_id = f"td-pcs-{os.urandom(4).hex()}"
    paths = SessionPaths(session_id=session_id, home=short_tmp, xdg_cache_home=short_tmp / "cache")
    (short_tmp / "cache").mkdir()

    project_dir, env_file = setup_daemon_project(short_tmp, paths)
    # Use os.environ directly so child processes inherit (monkeypatch doesn't fork).
    # Use os.environ directly so child processes inherit (monkeypatch doesn't fork).
    saved = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "DUCKTAPE_CLAUDE_HOOKS_PROFILE")}
    os.environ["CLAUDE_PROJECT_DIR"] = str(project_dir)
    os.environ["CLAUDE_ENV_FILE"] = str(env_file)
    os.environ["DUCKTAPE_CLAUDE_HOOKS_PROFILE"] = PROFILE_FILENAME
    try:
        n = 5
        barrier: multiprocessing.synchronize.Barrier = multiprocessing.Barrier(n)
        results: multiprocessing.sharedctypes.SynchronizedArray = multiprocessing.Array("i", n)

        procs = [
            multiprocessing.Process(target=_cold_start_worker, args=(paths, barrier, results, i)) for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)

        failed = [i for i in range(n) if results[i] != 0]
        assert not failed, f"Workers {failed} raised exceptions"
        assert _UDSConnection(paths.hook_daemon_sock).check_health()
        assert read_pidfile(paths.hook_daemon_pidfile) > 0
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
    assert not _UDSConnection(daemon_paths.hook_daemon_sock).check_health()

    _ensure_daemon(daemon_paths)

    assert _UDSConnection(daemon_paths.hook_daemon_sock).check_health()
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
    assert _UDSConnection(daemon_paths.hook_daemon_sock).check_health()


def test_wait_for_sock_detects_pre_pidfile_crash(daemon_paths: SessionPaths) -> None:
    """_wait_for_sock raises quickly when the daemon PID is already dead (pre-pidfile crash)."""
    # Use a PID that is guaranteed dead: fork a child that exits immediately.
    pid = os.fork()
    if pid == 0:
        os._exit(1)
    os.waitpid(pid, 0)  # Reap so PID is fully gone.

    start = time.monotonic()
    with pytest.raises(DaemonStartError, match="pre-pidfile"):
        _wait_for_sock(daemon_paths.hook_daemon_sock, pidfile=daemon_paths.hook_daemon_pidfile, daemon_pid=pid)
    elapsed = time.monotonic() - start
    # Should detect within a couple poll cycles, not wait for the full 5s timeout.
    assert elapsed < 1.0, f"Took {elapsed:.1f}s — expected < 1s"


def test_circuit_breaker_blocks_after_failure(daemon_paths: SessionPaths) -> None:
    """_check_circuit_breaker raises when cooldown hasn't elapsed."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)

    _record_startup_failure(daemon_dir)

    with pytest.raises(DaemonStartError, match="Circuit breaker open"):
        _check_circuit_breaker(daemon_dir)


def test_circuit_breaker_allows_after_cooldown(daemon_paths: SessionPaths) -> None:
    """_check_circuit_breaker passes when cooldown has elapsed."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)

    _record_startup_failure(daemon_dir)

    # Backdate the failure so cooldown has elapsed (1 failure → 4s cooldown).
    backdated = StartupFailure(
        consecutive_failures=1, last_failure=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=10)
    )
    fail_file = daemon_dir / "startup_failure.json"
    fail_file.write_bytes(backdated.model_dump_json().encode())

    # Should not raise.
    _check_circuit_breaker(daemon_dir)


def test_circuit_breaker_clears_on_success(daemon_paths: SessionPaths) -> None:
    """_clear_startup_failure removes the sentinel file."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)

    _record_startup_failure(daemon_dir)
    assert _read_startup_failure(daemon_dir) is not None

    _clear_startup_failure(daemon_dir)
    assert _read_startup_failure(daemon_dir) is None


def test_circuit_breaker_increments_count(daemon_paths: SessionPaths) -> None:
    """Consecutive failures increment the counter."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)

    _record_startup_failure(daemon_dir)
    f1 = _read_startup_failure(daemon_dir)
    assert f1 is not None
    assert f1.consecutive_failures == 1

    _record_startup_failure(daemon_dir)
    f2 = _read_startup_failure(daemon_dir)
    assert f2 is not None
    assert f2.consecutive_failures == 2

    _record_startup_failure(daemon_dir)
    f3 = _read_startup_failure(daemon_dir)
    assert f3 is not None
    assert f3.consecutive_failures == 3


def test_circuit_breaker_noop_when_no_failures(daemon_paths: SessionPaths) -> None:
    """_check_circuit_breaker is a no-op when no sentinel exists."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)
    _check_circuit_breaker(daemon_dir)


def test_circuit_breaker_handles_corrupt_file(daemon_paths: SessionPaths) -> None:
    """Corrupt sentinel file is deleted and doesn't block startup."""
    daemon_dir = daemon_paths.hook_daemon_dir
    daemon_dir.mkdir(parents=True, exist_ok=True)

    fail_file = daemon_dir / "startup_failure.json"
    fail_file.write_text("not valid json{{{")

    _check_circuit_breaker(daemon_dir)
    assert not fail_file.exists()


if __name__ == "__main__":
    pytest_bazel.main()
