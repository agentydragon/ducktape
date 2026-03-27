"""Bazel wrapper for Claude Code — sets up environment and execs bazel.

Mode-aware: in web mode (CLAUDE_CODE_REMOTE=true), writes fresh proxy
credentials and verifies the in-process auth proxy is running. In CLI mode,
passes through directly.
Both modes inject --bazelrc=<per-session-bazelrc> derived from the session dir.

Routes to the correct binary based on invocation name: if invoked as "bazelisk",
execs bazelisk; if invoked as "bazel", execs bazel. The shell wrapper sets
_BAZEL_WRAPPER_NAME from basename($0).

Reads configuration from environment variables set by session_start.py.
"""

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from devinfra.claude.auth_proxy.credentials import check_credential_expiry
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS, get_upstream_proxy_url
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.env_file import ENV_BAZELISK_PATH
from devinfra.claude.errors import AuthProxyError
from devinfra.claude.hook_daemon.client import update_proxy_creds
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import ENV_SESSION_DIR, is_web_mode

logger = logging.getLogger(__name__)

# Set by the shell wrapper script from basename($0) and dirname($0)
_WRAPPER_NAME_ENV = "_BAZEL_WRAPPER_NAME"
_WRAPPER_DIR_ENV = "_BAZEL_WRAPPER_DIR"


def _invocation_name() -> str:
    """Determine the binary name this wrapper was invoked as (bazel or bazelisk)."""
    return os.environ.get(_WRAPPER_NAME_ENV, "bazel")


def warn_if_credentials_expiring() -> None:
    """Check JWT expiry from current HTTPS_PROXY env var and log warning if concerning."""
    upstream_url = get_upstream_proxy_url()
    if not upstream_url:
        return

    status = check_credential_expiry(upstream_url)

    if status.expiry is None:
        return

    minutes_remaining = (status.expiry - datetime.now(UTC)).total_seconds() / 60

    if minutes_remaining <= 0:
        logger.warning(
            "JWT EXPIRED (%.0f min ago). Start a new Claude Code session for fresh credentials", -minutes_remaining
        )
    elif minutes_remaining < 30:
        logger.info("JWT valid for %.0f min", minutes_remaining)


def _setup_logging(paths: SessionPaths) -> None:
    """Configure logging to both stderr and file.

    File logging persists even if the subprocess is killed (e.g., by test timeout),
    making it available for artifact collection.
    """
    formatter = logging.Formatter("[bazel-wrapper] %(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    # Stderr: only warnings and errors (keep output quiet on happy path)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)

    # File: verbose (DEBUG+) for post-mortem debugging
    log_file = paths.sandbox_writable_dir / "bazel-wrapper.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(stderr_handler)
    root_logger.addHandler(file_handler)

    # Show log file path on stderr so users know where to look
    print(f"[bazel-wrapper] log: {log_file}", file=sys.stderr)
    logger.info("bazel_wrapper started")


def _resolve_real_binary() -> str:
    """Resolve the real bazel/bazelisk binary path.

    Web mode: reads BAZELISK_PATH (set by session hook to Nix-provided bazelisk).
    CLI mode: finds the binary matching the invocation name (bazel or bazelisk)
    on PATH, skipping our own wrapper directory.
    """
    env_path = os.environ.get(ENV_BAZELISK_PATH)
    if env_path:
        path = Path(env_path)
        if not path.exists():
            raise FileNotFoundError(f"{ENV_BAZELISK_PATH}={env_path} does not exist")
        return env_path

    # CLI mode: find the real binary matching our invocation name
    invoked_as = _invocation_name()
    wrapper_dir = os.environ.get(_WRAPPER_DIR_ENV, "")
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if wrapper_dir and Path(directory).resolve() == Path(wrapper_dir).resolve():
            continue
        candidate = Path(directory) / invoked_as
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    raise FileNotFoundError(f"No {invoked_as} found on PATH")


def _refresh_proxy_creds(paths: SessionPaths) -> str:
    """Send fresh JWT credentials to the hook daemon's in-process auth proxy via RPC.

    Returns the local proxy URL (e.g. http://localhost:<port>).
    """
    https_proxy = get_upstream_proxy_url()
    if not https_proxy:
        raise AuthProxyError("No HTTPS_PROXY environment variable set")
    try:
        return update_proxy_creds(https_proxy, paths)
    except OSError as e:
        raise AuthProxyError(
            f"Auth proxy RPC failed: {e}. The hook daemon may not be running. "
            f"See AGENTS.md 'Recovering from a Broken Session Start Hook' for recovery steps."
        ) from e


def _run(paths: SessionPaths) -> None:
    if is_web_mode():
        local_proxy = _refresh_proxy_creds(paths)
        warn_if_credentials_expiring()
        # CLEANUP(2026-03-26): Remove TCP proxy branch once UDS mode is confirmed stable.
        # In TCP mode, override HTTPS_PROXY to point at the local auth proxy so
        # all JVM HTTP traffic goes through it. In UDS mode, gRPC goes through
        # --remote_proxy UDS and BCR uses the native JAVA_TOOL_OPTIONS proxy,
        # so no env var override is needed.
        if local_proxy != "uds-only":
            for var in PROXY_ENV_VARS:
                os.environ[var] = local_proxy

    real_binary = _resolve_real_binary()

    logger.info("Execing %s (invoked as %s)", real_binary, _invocation_name())
    os.execvp(real_binary, [real_binary, f"--bazelrc={paths.bazelrc}", *sys.argv[1:]])


def main() -> None:
    """Main entry point."""
    session_dir_str = os.environ.get(ENV_SESSION_DIR)
    if not session_dir_str:
        raise RuntimeError(f"{ENV_SESSION_DIR} environment variable is required")
    session_id = Path(session_dir_str).name
    paths = SessionPaths.from_env(session_id, dict(os.environ))

    _setup_logging(paths)
    log_entrypoint_debug("bazel_wrapper")

    try:
        _run(paths)
    except AuthProxyError as e:
        logger.exception("%s. The hook daemon may need restarting — start a new session or re-trigger hooks", e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
