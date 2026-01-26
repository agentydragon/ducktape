"""Bazel wrapper for Claude Code web - sets proxy env vars and ensures services running.

Reads configuration from environment variables set by bazelisk_setup.py.
Provides auto-recovery: restarts supervisor and proxy if not running.
"""

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from tools.claude_hooks import proxy_setup
from tools.claude_hooks.errors import BazelProxyError, MissingEnvVarError
from tools.claude_hooks.proxy_credentials import check_credential_expiry
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import ENV_BAZEL_PROXY_PORT, ENV_SUPERVISOR_PORT, HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient

logger = logging.getLogger(__name__)


def _log_debug_info(settings: HookSettings) -> None:
    """Log debug info about environment and settings for diagnosing issues."""
    supervisor_port = settings.get_supervisor_port()
    bazel_proxy_port = settings.get_bazel_proxy_port()
    supervisor_dir = settings.get_supervisor_dir()
    pidfile = settings.get_supervisor_pidfile()

    logger.info("=== bazel_wrapper debug info ===")
    logger.info("sys.executable: %s", sys.executable)
    logger.info("supervisor_port (from settings): %d", supervisor_port)
    logger.info("bazel_proxy_port (from settings): %d", bazel_proxy_port)
    logger.info("supervisor_dir: %s", supervisor_dir)
    logger.info("pidfile: %s (exists=%s)", pidfile, pidfile.exists())

    # Log relevant env vars
    env_supervisor_port = os.environ.get(ENV_SUPERVISOR_PORT)
    env_bazel_proxy_port = os.environ.get(ENV_BAZEL_PROXY_PORT)
    env_xdg_cache = os.environ.get("XDG_CACHE_HOME")
    env_xdg_config = os.environ.get("XDG_CONFIG_HOME")
    logger.info("env %s: %s", ENV_SUPERVISOR_PORT, env_supervisor_port)
    logger.info("env %s: %s", ENV_BAZEL_PROXY_PORT, env_bazel_proxy_port)
    logger.info("env XDG_CACHE_HOME: %s", env_xdg_cache)
    logger.info("env XDG_CONFIG_HOME: %s", env_xdg_config)

    if pidfile.exists():
        try:
            pid_content = pidfile.read_text().strip()
            logger.info("pidfile content: %s", pid_content)
            pid = int(pid_content)
            try:
                os.kill(pid, 0)
                logger.info("process %d: alive", pid)
            except ProcessLookupError:
                logger.info("process %d: not found", pid)
            except PermissionError:
                logger.info("process %d: permission denied", pid)
        except (ValueError, OSError) as e:
            logger.info("pidfile read error: %s", e)

    logger.info("=== end debug info ===")


def warn_if_credentials_expiring(settings: HookSettings) -> None:
    """Check JWT expiry and log warning if concerning."""
    creds_file = settings.get_bazel_creds_file()
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
    formatter = logging.Formatter("[bazel-proxy] %(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    # Always log to stderr
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    # Also log to file in supervisor directory (persists on timeout)
    log_file = settings.get_supervisor_dir() / "bazel-wrapper.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stderr_handler)
    root_logger.addHandler(file_handler)

    logger.info("bazel_wrapper started, log file: %s", log_file)


def main() -> None:
    """Main entry point."""
    settings = HookSettings()

    # Set up logging early so all debug info is captured to file
    _setup_logging(settings)

    # Log debug info to help diagnose wheel mode timeout issues
    _log_debug_info(settings)

    try:
        logger.info("Calling ensure_proxy_running...")
        proxy_setup.ensure_proxy_running(settings, SupervisorClient(settings))
        logger.info("ensure_proxy_running completed successfully")
        warn_if_credentials_expiring(settings)
    except BazelProxyError as e:
        logger.error("%s", e)
        logger.info("To restart: run the session_start hook again")
        logger.info("Logs: %s/bazel-proxy.{log,err.log}", settings.get_supervisor_dir())
        raise SystemExit(1) from e

    local_proxy = os.environ.get("BAZEL_LOCAL_PROXY")
    if not local_proxy:
        raise MissingEnvVarError("BAZEL_LOCAL_PROXY")
    for var in PROXY_ENV_VARS:
        os.environ[var] = local_proxy

    bazelrc_path = os.environ.get("BAZEL_PROXY_BAZELRC")
    if not bazelrc_path:
        raise MissingEnvVarError("BAZEL_PROXY_BAZELRC")

    bazelisk_path = os.environ.get("BAZELISK_PATH")
    if not bazelisk_path or not Path(bazelisk_path).exists():
        raise MissingEnvVarError("BAZELISK_PATH")

    os.execvp(bazelisk_path, [bazelisk_path, f"--bazelrc={bazelrc_path}", *sys.argv[1:]])


if __name__ == "__main__":
    main()
