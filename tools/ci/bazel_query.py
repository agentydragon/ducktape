"""Shared Bazel query utilities for CI scripts."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _run_bazel_query_cmd(cmd: list[str | Path], query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel query command using --query_file to avoid "Argument list too long" errors."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".query", delete=True) as f:
        f.write(query)
        f.flush()
        return subprocess.run([*cmd, f"--query_file={f.name}"], check=check, capture_output=True, text=True)


def run_query(query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel query using --query_file to avoid "Argument list too long" errors."""
    return _run_bazel_query_cmd(["bazelisk", "query"], query, check=check)


def run_cquery(query: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bazel cquery using --query_file.

    cquery respects target_compatible_with constraints, unlike query.
    """
    return _run_bazel_query_cmd(["bazelisk", "cquery", "--output=label"], query, check=check)


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
