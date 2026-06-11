"""Bazel wrapper for Claude Code web - sets proxy env vars and ensures services running.

Reads configuration from environment variables set by bazelisk_setup.py.
Provides auto-recovery: restarts supervisor and proxy if not running.
"""

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime

from tools.claude_hooks import proxy_setup
from tools.claude_hooks.debug import log_entrypoint_debug
from tools.claude_hooks.env_file import ENV_AUTH_PROXY_BAZELRC, ENV_AUTH_PROXY_URL, ENV_BAZELISK_PATH
from tools.claude_hooks.errors import AuthProxyError
from tools.claude_hooks.proxy_credentials import check_credential_expiry
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient
from tools.env_utils import get_required_env, get_required_existing_path

logger = logging.getLogger(__name__)


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
    formatter = logging.Formatter("[auth-proxy] %(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

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

    log_entrypoint_debug("bazel_wrapper")

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

    bazelrc_path = get_required_env(ENV_AUTH_PROXY_BAZELRC)
    bazelisk_path = str(get_required_existing_path(ENV_BAZELISK_PATH))

    os.execvp(bazelisk_path, [bazelisk_path, f"--bazelrc={bazelrc_path}", *sys.argv[1:]])


if __name__ == "__main__":
    main()
