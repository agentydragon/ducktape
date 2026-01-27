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


def _run_bazel_query_cmd(cmd: list[str | Path], query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel query command using --query_file to avoid "Argument list too long" errors.

    Query files are saved to BAZEL_QUERY_LOG_DIR for CI artifact capture on failure.
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

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

    return result


def run_query(query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel query using --query_file to avoid "Argument list too long" errors."""
    return _run_bazel_query_cmd(["bazelisk", "query"], query, check=check)


def run_cquery(query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel cquery using --query_file.

    cquery respects target_compatible_with constraints, unlike query.
    Uses starlark output to get clean labels without configuration hash suffix.
    """
    return _run_bazel_query_cmd(
        ["bazelisk", "cquery", "--output=starlark", "--starlark:expr=target.label"], query, check=check
    )


def check_bazel_intersection(targets: list[str], pattern: str) -> bool:
    """Check if affected targets intersect with a Bazel pattern."""
    if not targets:
        return False

    query = f"set({' '.join(targets)}) intersect {pattern}"
    result = run_query(query)
    return bool(result.stdout.strip())


def filter_compatible_targets(targets: list[str]) -> list[str]:
    """Filter targets to only those compatible with the current platform.

    Uses bazel cquery which respects target_compatible_with constraints.
    """
    if not targets:
        return targets

    query = f"set({' '.join(targets)})"
    result = run_cquery(query, check=False)

    if result.returncode != 0:
        # cquery failed - return original targets
        return targets

    return [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]


def filter_to_rules(targets: list[str]) -> list[str]:
    """Filter targets to only rule targets (exclude source files).

    bazel-diff can return source file labels like //:foo.py or //pkg:BUILD.bazel.
    These are valid Bazel labels but cannot be built directly - only rule targets
    (py_library, py_test, etc.) can be built. This filters the list to keep only
    buildable rule targets.
    """
    if not targets:
        return targets

    query = f"kind('rule', set({' '.join(targets)}))"
    result = run_query(query, check=False)

    if result.returncode != 0:
        # Query failed - return original targets (will fail at build time with better error)
        return targets

    return [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
