"""Thin UDS client for the hook daemon with self-healing.

Sends hook RPCs to the daemon over a Unix domain socket. If the daemon is
unreachable, forks a new one and retries. Uses only stdlib (urllib) on the
hot path to avoid importing httpx.
"""

import fcntl
import http.client
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from filelock import FileLock

from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.hook_daemon.models import HookRequest, HookResponse, UpdateProxyCredsResponse
from devinfra.claude.session_paths import SessionPaths
from util.bazel.subprocess import python_env

logger = logging.getLogger(__name__)

# How long to wait for the daemon socket to appear after starting the daemon.
_DAEMON_STARTUP_TIMEOUT_SECS = 15


class _UDSConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects to a Unix domain socket."""

    def __init__(self, sock_path: Path) -> None:
        # host is unused but required by HTTPConnection
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self._sock_path))


class DaemonStartError(RuntimeError):
    """Raised when the hook daemon fails to start or crashes during startup."""


def update_proxy_creds(https_proxy: str, paths: SessionPaths) -> str:
    """Send fresh proxy credentials to the daemon. Returns the local proxy URL.

    Raises OSError if the daemon is unreachable.
    """
    sock_path = paths.hook_daemon_sock
    payload = json.dumps({"https_proxy": https_proxy}).encode()
    conn = _UDSConnection(sock_path)
    conn.timeout = 5.0
    conn.request("POST", "/update-proxy-creds", body=payload, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    if response.status != 200:
        raise OSError(f"Daemon returned HTTP {response.status} for update-proxy-creds: {body.decode()}")
    return UpdateProxyCredsResponse.model_validate_json(body).proxy_url


def check_health(sock_path: Path, timeout: float = 0.5) -> bool:
    """Check if the daemon is healthy by hitting GET /health. Returns False on any failure."""
    try:
        conn = _UDSConnection(sock_path)
        conn.timeout = timeout
        conn.request("GET", "/health")
        response = conn.getresponse()
        conn.close()
        return response.status == 200
    except (ConnectionRefusedError, FileNotFoundError, OSError, http.client.HTTPException):
        return False


def call_daemon(hook_input: AnyHookInput, env: dict[str, str], paths: SessionPaths) -> HookResponse | None:
    """POST to daemon over UDS. If unreachable, start daemon and retry.

    Raises DaemonStartError if the daemon process dies during startup.
    """
    sock_path = paths.hook_daemon_sock
    request = HookRequest(hook=hook_input, env=env)

    # Fast path: talk to an existing healthy daemon without any locking.
    if sock_path.exists():
        result = _post_to_daemon(request, sock_path)
        if result is not None:
            return result
        logger.info("Daemon unreachable on existing socket %s, will restart", sock_path)

    # Slow path: ensure a healthy daemon exists (may kill a stale/hung one
    # and start a fresh one), then retry.
    _ensure_daemon(paths)
    return _post_to_daemon(request, sock_path)


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


def read_pidfile(pidfile: Path) -> int:
    """Read PID from a pidfile. Raises ValueError/OSError if unreadable."""
    return int(pidfile.read_text().strip())


def _is_pidfile_locked(pidfile: Path) -> bool:
    """Non-blocking flock probe on the pidfile. Returns True if a daemon holds the lock.

    Uses raw fcntl.flock — filelock.FileLock.release() unlinks the file, which
    would destroy the daemon's PID data.
    """
    try:
        fd = os.open(str(pidfile), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _kill_daemon_by_pidfile(pidfile: Path) -> None:
    """Kill the daemon identified by pidfile: SIGTERM, short grace, then SIGKILL.

    The flock on the pidfile is authoritative for liveness — no PID-reuse ambiguity.
    With double-fork daemonization the daemon is reparented to init, so no zombies.
    """
    try:
        pid = read_pidfile(pidfile)
    except (ValueError, OSError) as e:
        logger.warning("Cannot read PID from %s: %s", pidfile, e)
        return

    logger.info("Killing daemon (pid=%d): sending SIGTERM", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    # Short grace period for graceful shutdown, then force-kill.
    time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return  # Already dead from SIGTERM.
    except OSError as e:
        logger.warning("Failed to SIGKILL pid=%d: %s", pid, e)

    # Wait for process to fully exit.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    logger.warning("Daemon pid=%d still alive after SIGTERM+SIGKILL", pid)


def _ensure_daemon(paths: SessionPaths) -> None:
    """Ensure a healthy daemon is running, starting or restarting as needed.

    Holds daemon.lock for the entire duration — from checking liveness through
    forking and waiting for the new daemon's socket to accept connections. This
    prevents concurrent clients from racing to start multiple daemons (Bazel
    server pattern).

    Daemon liveness is determined by an exclusive flock on daemon.pid, held by
    the daemon for its entire lifetime. The kernel releases it on process death,
    so the probe is authoritative regardless of PID reuse.
    """
    daemon_dir = paths.hook_daemon_dir
    sock_path = paths.hook_daemon_sock
    pidfile = paths.hook_daemon_pidfile

    # Create daemon dir before acquiring the lock (FileLock needs the parent to exist).
    daemon_dir.mkdir(parents=True, exist_ok=True)

    with FileLock(str(daemon_dir / "daemon.lock")):
        # Re-check after acquiring: another client may have won the race and
        # already started a healthy daemon while we were waiting.
        if check_health(sock_path):
            logger.debug("Daemon already healthy (socket=%s), skipping start", sock_path)
            return

        # Probe daemon liveness via flock on pidfile.
        if _is_pidfile_locked(pidfile):
            # Daemon process is alive (holds the flock), but health check
            # failed above — it's hung. Kill it.
            _kill_daemon_by_pidfile(pidfile)
        else:
            logger.debug("Pidfile lock available — no live daemon")

        paths.ensure_dirs()

        # Clean stale state from previous daemon before starting a fresh one.
        if sock_path.exists():
            logger.debug("Removing stale socket %s", sock_path)
            sock_path.unlink()
        if pidfile.exists():
            pidfile.unlink()

        _fork_daemon(daemon_dir, sock_path)

        # Wait for socket while still holding daemon.lock — prevents other
        # clients from entering and trying to start a second daemon.
        _wait_for_sock(sock_path, pidfile=pidfile, daemon_dir=daemon_dir)


def _fork_daemon(daemon_dir: Path, sock_path: Path) -> None:
    """Fork the daemon as a double-forked background process.

    Uses the classic Unix double-fork pattern: parent → child → grandchild.
    The intermediate child exits immediately (reaped synchronously by the parent),
    and the grandchild is reparented to init. This eliminates zombies — unlike
    Popen(start_new_session=True) where the parent must waitpid to reap.
    """
    daemon_module = "devinfra.claude.hook_daemon.main"
    log_out = daemon_dir / "daemon.log"
    log_err = daemon_dir / "daemon.err.log"

    logger.info("Starting daemon: module=%s sock=%s daemon_dir=%s", daemon_module, sock_path, daemon_dir)

    pid = os.fork()
    if pid > 0:
        # Parent: reap intermediate child immediately (it exits right away).
        os.waitpid(pid, 0)
        return

    # Intermediate child: new session, then fork again.
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)  # Intermediate child exits.

    # Grandchild: this becomes the daemon process.
    # Redirect stdout/stderr to log files.
    fd_out = os.open(str(log_out), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    fd_err = os.open(str(log_err), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd_out, 1)
    os.dup2(fd_err, 2)
    os.close(fd_out)
    os.close(fd_err)

    env = python_env()
    os.execve(
        sys.executable,
        [sys.executable, "-m", daemon_module, "--sock", str(sock_path), "--daemon-dir", str(daemon_dir)],
        env,
    )


def _read_daemon_error_log(daemon_dir: Path) -> str:
    """Read the last lines of daemon error/stdout logs for diagnostics."""
    parts: list[str] = []
    for name in ("daemon.err.log", "daemon.log"):
        log_file = daemon_dir / name
        if log_file.exists():
            content = log_file.read_text().strip()
            if content:
                parts.append(f"--- {name} ---\n{content[-2000:]}")
    return "\n".join(parts) if parts else "(no daemon logs found)"


def _wait_for_sock(
    sock_path: Path, *, pidfile: Path, daemon_dir: Path, timeout_secs: float = _DAEMON_STARTUP_TIMEOUT_SECS
) -> None:
    """Poll until socket file exists and accepts connections.

    Detects daemon crashes via flock probe on the pidfile: if the lock becomes
    available, the daemon died (kernel released the flock on process exit).
    """
    logger.debug("Waiting for daemon socket %s (timeout=%.1fs)", sock_path, timeout_secs)
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(sock_path))
                s.close()
                logger.debug("Daemon socket ready at %s", sock_path)
                return
            except (ConnectionRefusedError, OSError):
                pass

        # Crash detection: if the daemon died, its flock on the pidfile is
        # released by the kernel.  The stale pidfile is deleted before forking,
        # so if it exists, the new daemon created it.  An unlocked pidfile means
        # the daemon died after creating the file but before (or after) binding.
        if pidfile.exists() and not _is_pidfile_locked(pidfile):
            raise DaemonStartError(f"Daemon died during startup.\n{_read_daemon_error_log(daemon_dir)}")

        time.sleep(0.1)
    logger.warning("Daemon socket did not appear within %.1fs at %s", timeout_secs, sock_path)
    raise DaemonStartError(
        f"Daemon socket did not appear within {timeout_secs}s.\n{_read_daemon_error_log(daemon_dir)}"
    )
