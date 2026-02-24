"""Bazel wrapper for Claude Code — sets up environment and execs bazel.

Mode-aware: in web mode (CLAUDE_CODE_REMOTE=true), sets proxy env vars and
ensures auth proxy is running. In CLI mode, passes through directly.
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

from tools.claude_hooks import proxy_setup
from tools.claude_hooks.debug import log_entrypoint_debug
from tools.claude_hooks.env_file import ENV_AUTH_PROXY_URL, ENV_BAZELISK_PATH, ENV_SESSION_BAZELRC
from tools.claude_hooks.errors import AuthProxyError
from tools.claude_hooks.proxy_credentials import check_credential_expiry
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient
from util.env import get_required_env, get_required_existing_path

logger = logging.getLogger(__name__)

# Set by the shell wrapper script from basename($0) and dirname($0)
_WRAPPER_NAME_ENV = "_BAZEL_WRAPPER_NAME"
_WRAPPER_DIR_ENV = "_BAZEL_WRAPPER_DIR"


def _is_web_mode() -> bool:
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


def _invocation_name() -> str:
    """Determine the binary name this wrapper was invoked as (bazel or bazelisk)."""
    return os.environ.get(_WRAPPER_NAME_ENV, "bazel")


def warn_if_credentials_expiring(settings: HookSettings) -> None:
    """Check JWT expiry and log warning if concerning."""
    creds_file = settings.get_auth_proxy_creds_file()
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


def _setup_logging(settings: HookSettings) -> None:
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
    log_file = settings.get_sandbox_writable_dir() / "bazel-wrapper.log"
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
        return str(get_required_existing_path(ENV_BAZELISK_PATH))

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


def main() -> None:
    """Main entry point."""
    settings = HookSettings()

    _setup_logging(settings)
    log_entrypoint_debug("bazel_wrapper")

    if _is_web_mode():
        try:
            logger.info("Calling ensure_proxy_running...")
            asyncio.run(proxy_setup.ensure_proxy_running(settings, SupervisorClient(settings)))
            logger.info("ensure_proxy_running completed successfully")
            warn_if_credentials_expiring(settings)
        except AuthProxyError as e:
            logger.error("%s", e)
            logger.info("To restart: run the session_start hook again")
            logger.info("Logs: %s/auth-proxy.{log,err.log}", settings.get_supervisor_dir())
            raise SystemExit(1) from e

        local_proxy = get_required_env(ENV_AUTH_PROXY_URL)
        for var in PROXY_ENV_VARS:
            os.environ[var] = local_proxy

    # SESSION_BAZELRC is the canonical env var. Fall back to AUTH_PROXY_BAZELRC
    # for sessions started before the migration (old hook code set that instead).
    # TODO(unify-web-cli): Remove AUTH_PROXY_BAZELRC fallback once all sessions
    # have been restarted with the new hook code that sets SESSION_BAZELRC.
    bazelrc_path = os.environ.get(ENV_SESSION_BAZELRC) or get_required_env("AUTH_PROXY_BAZELRC")
    real_binary = _resolve_real_binary()

    logger.info("Execing %s (invoked as %s)", real_binary, _invocation_name())
    os.execvp(real_binary, [real_binary, f"--bazelrc={bazelrc_path}", *sys.argv[1:]])


if __name__ == "__main__":
    main()
