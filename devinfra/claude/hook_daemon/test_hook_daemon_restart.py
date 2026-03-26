"""E2E test: daemon restart after crash.

Tests the client's self-healing behavior when the daemon process dies.
Uses in-process uvicorn servers on UDS to avoid Bazel subprocess PYTHONPATH issues.
"""

import socket
import threading
import time
from pathlib import Path

import pytest_bazel
import uvicorn

from devinfra.claude.claude_api.hooks.stop import StopInput
from devinfra.claude.hook_daemon.client import _post_to_daemon
from devinfra.claude.hook_daemon.models import HookRequest
from devinfra.claude.hook_daemon.server import app, configure
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter

_COMMON = {
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
}


def _start_uvicorn_in_thread(sock_path: Path, daemon_dir: Path) -> uvicorn.Server:
    """Start uvicorn serving the daemon app on a UDS in a background thread."""
    configure(daemon_dir, DeferredOtlpExporter())
    config = uvicorn.Config(app=app, uds=str(sock_path), log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(sock_path))
                s.close()
                return server
            except (ConnectionRefusedError, OSError):
                pass
        time.sleep(0.1)
    raise TimeoutError("Uvicorn did not start within 10s")


def _make_request() -> HookRequest:
    hook_input = StopInput(**_COMMON, hook_event_name="Stop", stop_hook_active=True, last_assistant_message="test")
    return HookRequest(hook=hook_input, env={"HOME": "/tmp", "PATH": "/usr/bin"})


def test_daemon_serves_over_uds(short_tmp: Path) -> None:
    """Basic e2e: start daemon on UDS, send RPC, get response."""
    daemon_dir = short_tmp / "hd"
    daemon_dir.mkdir()
    sock_path = daemon_dir / "daemon.sock"

    server = _start_uvicorn_in_thread(sock_path, daemon_dir)
    try:
        result = _post_to_daemon(_make_request(), sock_path)
        assert result is not None
        assert result.output is None  # Stop is a noop

        # Env should be persisted
        env_file = daemon_dir / "session_env.json"
        assert env_file.exists()
    finally:
        server.should_exit = True


def test_client_detects_dead_daemon(short_tmp: Path) -> None:
    """Client returns None when daemon socket is gone (simulating crash)."""
    sock_path = short_tmp / "nonexistent.sock"

    # No daemon running, socket doesn't exist
    result = _post_to_daemon(_make_request(), sock_path)
    assert result is None


def test_daemon_restart_recovery(short_tmp: Path) -> None:
    """After first daemon dies, a new one can serve on the same socket path."""
    daemon_dir = short_tmp / "hd"
    daemon_dir.mkdir()
    sock_path = daemon_dir / "daemon.sock"

    # Start first daemon
    server1 = _start_uvicorn_in_thread(sock_path, daemon_dir)

    # Verify it works
    result1 = _post_to_daemon(_make_request(), sock_path)
    assert result1 is not None

    # Kill first daemon
    server1.should_exit = True
    time.sleep(1)  # Wait for shutdown

    # Clean up stale socket if present
    if sock_path.exists():
        sock_path.unlink()

    # Start second daemon on same path
    server2 = _start_uvicorn_in_thread(sock_path, daemon_dir)
    try:
        # Verify second daemon works
        result2 = _post_to_daemon(_make_request(), sock_path)
        assert result2 is not None
        assert result2.output is None
    finally:
        server2.should_exit = True


if __name__ == "__main__":
    pytest_bazel.main()
