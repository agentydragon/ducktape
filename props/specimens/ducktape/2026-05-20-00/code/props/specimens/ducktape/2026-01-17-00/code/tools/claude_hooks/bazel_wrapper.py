"""Bazel wrapper for Claude Code web - sets proxy env vars and ensures services running.

Reads configuration from environment variables set by bazelisk_setup.py.
Provides auto-recovery: restarts supervisor and proxy if not running.
"""

import logging
import os
import sys
from datetime import UTC, datetime

from claude_hooks import proxy_setup
from claude_hooks.errors import BazelProxyError, MissingEnvVarError
from claude_hooks.proxy_credentials import check_credential_expiry

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Get required environment variable."""
    value = os.environ.get(name)
    if not value:
        raise MissingEnvVarError(name)
    return value


def ensure_services_running() -> None:
    """Ensure supervisor and proxy are running, starting them if needed."""
    proxy_setup.ensure_proxy_running()


def warn_if_credentials_expiring() -> None:
    """Check JWT expiry and log warning if concerning."""
    creds_file = proxy_setup._get_bazel_creds_file()
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


def log_recovery_instructions(repo_path: str) -> None:
    """Log instructions for manual recovery."""
    logger.info("To restart: cd %s && python3 -m claude_hooks.session_start", repo_path)
    logger.info("Logs: ~/.config/supervisor/bazel-proxy.{log,err.log}")


def main() -> None:
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO, format="[bazel-proxy] %(message)s", handlers=[logging.StreamHandler(sys.stderr)]
    )

    try:
        ensure_services_running()
        warn_if_credentials_expiring()
    except BazelProxyError as e:
        logger.error("%s", e)
        log_recovery_instructions(require_env("DUCKTAPE_REPO_ROOT"))
        raise SystemExit(1) from e

    for var in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]:
        os.environ[var] = require_env("BAZEL_LOCAL_PROXY")

    bazelisk_path = require_env("BAZELISK_PATH")
    os.execvp(bazelisk_path, [bazelisk_path, *sys.argv[1:]])


if __name__ == "__main__":
    main()
