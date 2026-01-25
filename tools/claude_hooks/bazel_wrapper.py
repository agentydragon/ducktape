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
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient

logger = logging.getLogger(__name__)


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


def main() -> None:
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO, format="[bazel-proxy] %(message)s", handlers=[logging.StreamHandler(sys.stderr)]
    )

    settings = HookSettings()

    try:
        proxy_setup.ensure_proxy_running(settings, SupervisorClient(settings))
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
