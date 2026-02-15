"""Shared runfiles utilities for Bazel tests and scripts.

Provides helpers to locate binaries and data files in Bazel runfiles.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

try:
    from python.runfiles import runfiles
except ImportError:
    runfiles = None  # type: ignore[assignment]  # Not available outside Bazel (e.g. wheel installs)

try:
    from python.runfiles.runfiles import Runfiles
except ImportError:
    Runfiles = None  # type: ignore[misc,assignment]


@cache
def _get_runfiles() -> runfiles.Runfiles:
    """Get runfiles instance (lazily initialized, cached)."""
    if runfiles is None:
        raise RuntimeError("python.runfiles not available - are you running via Bazel?")
    r = runfiles.Create()
    if r is None:
        raise RuntimeError("Could not create runfiles - are you running via Bazel?")
    return r


def get_required_path(rlocation: str) -> Path:
    """Get path to a file or directory from runfiles, checking it exists.

    Args:
        rlocation: Runfiles path (e.g., "_main/tools/claude_hooks/session_start")

    Returns:
        Absolute Path to the file or directory.

    Raises:
        RuntimeError: If the path cannot be located or doesn't exist.
    """
    resolved = _get_runfiles().Rlocation(rlocation)
    if not resolved:
        raise RuntimeError(f"Could not resolve runfiles path: {rlocation}")
    path = Path(resolved)
    if not path.exists():
        raise RuntimeError(f"Resolved path does not exist: {path}")
    return path


def find_runfiles_files(pattern: str) -> list[Path]:
    """Find files in runfiles matching a glob pattern.

    Args:
        pattern: Glob pattern relative to runfiles root (e.g., "_main/props/specimens/**/issues/**/*.yaml")

    Returns:
        Sorted list of absolute Paths to matching files.
    """
    rf = _get_runfiles()

    # Get the base directory (part before first wildcard)
    parts = pattern.split("/")
    base_parts = []
    for part in parts:
        if "*" in part or "?" in part or "[" in part:
            break
        base_parts.append(part)

    if not base_parts:
        raise ValueError(f"Pattern must have at least one non-wildcard directory: {pattern}")

    base_rlocation = "/".join(base_parts)
    base_path = rf.Rlocation(base_rlocation)
    if not base_path:
        return []

    base_dir = Path(base_path)
    if not base_dir.exists():
        return []

    # Construct the glob pattern relative to base directory
    relative_pattern = "/".join(parts[len(base_parts) :])
    if not relative_pattern:
        return [base_dir] if base_dir.is_file() else []

    # Use Path.glob to find matching files
    matches = sorted(base_dir.glob(relative_pattern))
    return [m for m in matches if m.is_file()]
