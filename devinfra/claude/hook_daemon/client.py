"""Thin UDS client for the hook daemon with self-healing.

Sends hook RPCs to the daemon over a Unix domain socket. If the daemon is
unreachable, forks a new one and retries. Uses only stdlib (urllib) on the
hot path to avoid importing httpx.
"""

import contextlib
import http.client
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.hook_daemon.models import HookRequest, HookResponse
from devinfra.claude.session_paths import SessionPaths
from util.bazel.subprocess import python_env

logger = logging.getLogger(__name__)


class _UDSConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects to a Unix domain socket."""

    def __init__(self, sock_path: Path) -> None:
        # host is unused but required by HTTPConnection
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self._sock_path))


def call_daemon(hook_input: AnyHookInput, env: dict[str, str], paths: SessionPaths) -> HookResponse | None:
    """POST to daemon over UDS. If unreachable, start daemon and retry."""
    sock_path = paths.hook_daemon_sock
    request = HookRequest(hook=hook_input, env=env)

    if sock_path.exists():
        result = _post_to_daemon(request, sock_path)
        if result is not None:
            return result
        logger.info("Daemon unreachable on existing socket %s, will restart", sock_path)

    # Daemon unreachable or socket missing — start it
    if _start_daemon(paths):
        _wait_for_sock(sock_path, timeout_secs=30)
        return _post_to_daemon(request, sock_path)

    return None


def _post_to_daemon(request: HookRequest, sock_path: Path) -> HookResponse | None:
    """Send a hook request to the daemon. Returns None if connection fails."""
    try:
        conn = _UDSConnection(sock_path)
        payload = request.model_dump_json().encode()
        conn.request("POST", "/hook", body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            logger.warning("Daemon returned HTTP %d", response.status)
            return None
        body = response.read()
        conn.close()
        return HookResponse.model_validate_json(body)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
        logger.debug("Daemon unreachable: %s", e)
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _start_daemon(paths: SessionPaths) -> bool:
    """Fork daemon as a detached background process. Returns True if started."""
    daemon_dir = paths.hook_daemon_dir
    sock_path = paths.hook_daemon_sock
    pidfile = paths.hook_daemon_pidfile

    # Check if daemon is already running (pidfile with live process)
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            if _is_pid_alive(pid):
                # PID is alive but socket is gone — stale state, kill it
                if not sock_path.exists():
                    logger.info("Stale daemon (pid=%d, socket missing), killing", pid)
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.5)
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                    logger.info("Killed stale daemon pid=%d", pid)
                else:
                    logger.debug("Daemon already running (pid=%d, socket=%s)", pid, sock_path)
                    return True
            else:
                logger.info("Stale pidfile (pid=%d is dead), cleaning up", pid)
        except (ValueError, OSError) as e:
            logger.warning("Bad pidfile %s: %s", pidfile, e)

    # Create daemon dir and socket dir
    daemon_dir.mkdir(parents=True, exist_ok=True)
    paths.ensure_dirs()

    # Clean stale socket
    if sock_path.exists():
        logger.debug("Removing stale socket %s", sock_path)
        sock_path.unlink()

    # Fork daemon as detached subprocess
    daemon_module = "devinfra.claude.hook_daemon.main"
    log_out = daemon_dir / "daemon.log"
    log_err = daemon_dir / "daemon.err.log"

    logger.info("Starting daemon: module=%s sock=%s daemon_dir=%s", daemon_module, sock_path, daemon_dir)

    with log_out.open("a") as stdout_f, log_err.open("a") as stderr_f:
        proc = subprocess.Popen(
            [sys.executable, "-m", daemon_module, "--sock", sock_path, "--daemon-dir", daemon_dir],
            stdout=stdout_f,
            stderr=stderr_f,
            env=python_env(),
            start_new_session=True,  # Detach from parent
        )

    # Write pidfile
    pidfile.write_text(str(proc.pid))
    logger.info("Started daemon (pid=%d, sock=%s)", proc.pid, sock_path)
    return True


def _wait_for_sock(sock_path: Path, *, timeout_secs: float = 30) -> bool:
    """Poll until socket file exists and accepts connections."""
    logger.debug("Waiting for daemon socket %s (timeout=%.1fs)", sock_path, timeout_secs)
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(sock_path))
                s.close()
                logger.debug("Daemon socket ready at %s", sock_path)
                return True
            except (ConnectionRefusedError, OSError):
                pass
        time.sleep(0.1)
    logger.warning("Daemon socket did not appear within %.1fs at %s", timeout_secs, sock_path)
    return False
