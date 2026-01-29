"""Shared debug logging for claude_hooks entrypoints."""

from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


def log_entrypoint_debug(name: str) -> None:
    """Log standard debug info for an entrypoint.

    Logs sys.executable, sys.path, and full environment as JSON.
    Call early in each entrypoint (session_start, bazel_wrapper, auth_proxy).
    """
    logger.info("=== %s debug info ===", name)
    logger.info("sys.executable: %s", sys.executable)
    logger.info("sys.path: %s", sys.path)
    logger.info("Full environment:\n%s", json.dumps(dict(os.environ), sort_keys=True, indent=2))
    logger.info("=== end %s debug info ===", name)
