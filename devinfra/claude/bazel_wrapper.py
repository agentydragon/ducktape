"""Bazel wrapper for Claude Code — sets up environment and execs bazel.

Mode-aware: in web mode (CLAUDE_CODE_REMOTE=true), writes fresh proxy
credentials and verifies the in-process auth proxy is running. In CLI mode,
passes through directly.
Both modes inject --bazelrc=<per-session-bazelrc> via SESSION_BAZELRC.

Routes to the correct binary based on invocation name: if invoked as "bazelisk",
execs bazelisk; if invoked as "bazel", execs bazel. The shell wrapper sets
_BAZEL_WRAPPER_NAME from basename($0).

Reads configuration from environment variables set by session_start.py.
"""

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from devinfra.claude.auth_proxy.credentials import check_credential_expiry
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS, get_upstream_proxy_url
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.env_file import ENV_AUTH_PROXY_URL, ENV_BAZELISK_PATH, ENV_SESSION_BAZELRC
from devinfra.claude.errors import AuthProxyError
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import ENV_SESSION_DIR, HookSettings, is_web_mode
from util.env import get_required_env
from util.net import async_wait_for_port

logger = logging.getLogger(__name__)

# Set by the shell wrapper script from basename($0) and dirname($0)
_WRAPPER_NAME_ENV = "_BAZEL_WRAPPER_NAME"
_WRAPPER_DIR_ENV = "_BAZEL_WRAPPER_DIR"


def _invocation_name() -> str:
    """Determine the binary name this wrapper was invoked as (bazel or bazelisk)."""
    return os.environ.get(_WRAPPER_NAME_ENV, "bazel")


def warn_if_credentials_expiring(paths: SessionPaths) -> None:
    """Check JWT expiry and log warning if concerning."""
    creds_file = paths.auth_proxy_creds_file
    if not creds_file.exists():
        return

    status = check_credential_expiry(creds_file.read_text().strip())

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

    Web mode: reads BAZELISK_PATH (set by session hook to the downloaded bazelisk).
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


async def _ensure_proxy_creds_fresh(paths: SessionPaths, settings: HookSettings) -> None:
    """Write fresh credentials and verify the in-process auth proxy is listening.

    The auth proxy runs in the hook daemon process and reads its creds file
    on each connection. This function writes the current upstream proxy URL
    (which may have a refreshed JWT) to the daemon's creds file.
    """
    https_proxy = get_upstream_proxy_url()
    if not https_proxy:
        raise AuthProxyError("No HTTPS_PROXY environment variable set")

    creds_file = paths.auth_proxy_creds_file
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(https_proxy)
    logger.debug("Wrote fresh proxy credentials to %s", creds_file)

    # Verify proxy is listening
    try:
        await async_wait_for_port("127.0.0.1", settings.auth_proxy_port, timeout_secs=5.0)
    except TimeoutError as e:
        raise AuthProxyError(
            f"Auth proxy not listening on port {settings.auth_proxy_port}. "
            "The hook daemon may not be running. Try restarting your Claude Code session."
        ) from e


async def _async_main(paths: SessionPaths, settings: HookSettings) -> None:
    """Async entry point — all async work happens here."""
    if is_web_mode():
        await _ensure_proxy_creds_fresh(paths, settings)
        warn_if_credentials_expiring(paths)

        local_proxy = get_required_env(ENV_AUTH_PROXY_URL)
        for var in PROXY_ENV_VARS:
            os.environ[var] = local_proxy

    bazelrc_path = get_required_env(ENV_SESSION_BAZELRC)
    real_binary = _resolve_real_binary()

    logger.info("Execing %s (invoked as %s)", real_binary, _invocation_name())
    os.execvp(real_binary, [real_binary, f"--bazelrc={bazelrc_path}", *sys.argv[1:]])


def main() -> None:
    """Main entry point."""
    session_dir_str = os.environ.get(ENV_SESSION_DIR)
    if not session_dir_str:
        raise RuntimeError(f"{ENV_SESSION_DIR} environment variable is required")
    session_id = Path(session_dir_str).name
    paths = SessionPaths.from_env(session_id, dict(os.environ))
    settings = HookSettings()

    _setup_logging(paths)
    log_entrypoint_debug("bazel_wrapper")

    try:
        asyncio.run(_async_main(paths, settings))
    except AuthProxyError as e:
        logger.error("%s", e)
        logger.info("The hook daemon may need restarting — start a new session or re-trigger hooks")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
