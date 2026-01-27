"""Shared Bazel query utilities for CI scripts."""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_query_log_dir() -> Path:
    """Get the query log directory, reading env var at call time (not import time)."""
    return Path(os.environ.get("BAZEL_QUERY_LOG_DIR", "/tmp/bazel-query-logs"))


def _run_bazel_query_cmd(cmd: list[str | Path], query: str) -> list[str]:
    """Run a bazel query command using --query_file to avoid "Argument list too long" errors.

    Query files are saved to BAZEL_QUERY_LOG_DIR for CI artifact capture on failure.
    Returns list of targets from stdout.
    Raises CalledProcessError on failure.
    """
    # Save query to log directory for CI artifacts
    query_log_dir = _get_query_log_dir()
    logger.info(
        "Saving query to: %s (env BAZEL_QUERY_LOG_DIR=%s)", query_log_dir, os.environ.get("BAZEL_QUERY_LOG_DIR")
    )
    query_log_dir.mkdir(parents=True, exist_ok=True)
    # Each query gets its own subdirectory
    timestamp = datetime.now().strftime("%H%M%S")
    query_dir = query_log_dir / f"{timestamp}_{uuid.uuid4().hex[:8]}"
    query_dir.mkdir()
    query_file = query_dir / "query"
    query_file.write_text(query)

    result = subprocess.run([*cmd, f"--query_file={query_file}"], check=False, capture_output=True, text=True)

    (query_dir / "stdout").write_text(result.stdout)
    (query_dir / "stderr").write_text(result.stderr)
    (query_dir / "exit_code").write_text(str(result.returncode))

    if result.returncode != 0:
        logger.error("Query failed (exit %d). stderr:\n%s", result.returncode, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

    return [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]


def run_query(query: str) -> list[str]:
    """Run a bazel query and return matching targets.

    Raises CalledProcessError on failure.
    """
    return _run_bazel_query_cmd(["bazelisk", "query"], query)


def query_intersection(targets: list[str], pattern: str) -> list[str]:
    """Query targets that intersect with a pattern. Returns matching targets."""
    if not targets:
        return []

    query = f"set({' '.join(targets)}) intersect {pattern}"
    return run_query(query)


def filter_for_ci(targets: list[str]) -> list[str]:
    """Filter targets for CI: keep only buildable, compatible, non-manual targets.

    Combines three filters into a single bazel query invocation:
    - kind('rule', ...) — exclude source file labels (not buildable)
    - except attr(target_compatible_with, macos) — exclude platform-incompatible
    - except attr(tags, 'manual') — exclude targets needing special setup
      (e.g. system libraries). Release workflows build these explicitly.
    """
    if not targets:
        return targets

    target_set = f"set({' '.join(targets)})"
    query = (
        f"let targets = {target_set} in "
        f"kind('rule', $targets) "
        f"except attr(target_compatible_with, '@platforms//os:macos', $targets) "
        f"except attr(tags, 'manual', $targets)"
    )
    return run_query(query)
