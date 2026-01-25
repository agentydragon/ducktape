"""Shared runfiles utilities for Bazel tests and scripts.

Provides helpers to locate binaries and data files in Bazel runfiles.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from python.runfiles import runfiles


@cache
def _get_runfiles() -> runfiles.Runfiles:
    """Get runfiles instance (lazily initialized, cached)."""
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
