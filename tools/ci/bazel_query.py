"""Shared Bazel query utilities for CI scripts."""

from __future__ import annotations

import subprocess
import tempfile


def run_query_with_file(query: str, *, check: bool = False) -> subprocess.CompletedProcess:
    """Run a bazel query using --query_file to avoid "Argument list too long" errors.

    Args:
        query: The Bazel query string.
        check: If True, raise CalledProcessError on non-zero exit.

    Returns:
        CompletedProcess with stdout/stderr captured.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".query", delete_on_close=False) as f:
        f.write(query)
        f.flush()
        return subprocess.run(
            ["bazelisk", "query", f"--query_file={f.name}"], check=check, capture_output=True, text=True
        )


def run_cquery_with_file(query: str, *, check: bool = False) -> subprocess.CompletedProcess:
    """Run a bazel cquery using --query_file to avoid "Argument list too long" errors.

    cquery respects target_compatible_with constraints, unlike query.

    Args:
        query: The Bazel query string.
        check: If True, raise CalledProcessError on non-zero exit.

    Returns:
        CompletedProcess with stdout/stderr captured.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".query", delete_on_close=False) as f:
        f.write(query)
        f.flush()
        return subprocess.run(
            ["bazelisk", "cquery", f"--query_file={f.name}", "--output=label"],
            check=check,
            capture_output=True,
            text=True,
        )


def check_bazel_intersection(targets: list[str], pattern: str) -> bool:
    """Check if affected targets intersect with a Bazel pattern.

    Uses --query_file to avoid "Argument list too long" errors with large target sets.

    Args:
        targets: List of Bazel target labels.
        pattern: Bazel query pattern to intersect with.

    Returns:
        True if there's any intersection, False otherwise.
    """
    if not targets:
        return False

    targets_str = " ".join(targets)
    query = f"set({targets_str}) intersect {pattern}"
    result = run_query_with_file(query)
    return bool(result.stdout.strip())


def filter_compatible_targets(targets: list[str]) -> list[str]:
    """Filter targets to only those compatible with the current platform.

    Uses bazel cquery which respects target_compatible_with constraints.

    Args:
        targets: List of Bazel target labels.

    Returns:
        List of targets compatible with the current platform.
    """
    if not targets:
        return targets

    targets_str = " ".join(targets)
    query = f"set({targets_str})"
    result = run_cquery_with_file(query)

    if result.returncode != 0:
        # cquery failed - return original targets
        return targets

    return [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
