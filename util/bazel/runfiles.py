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
    """Resolve a runfiles path to an absolute Path, raising if missing."""
    if not (resolved := _get_runfiles().Rlocation(rlocation)):
        raise RuntimeError(f"Could not resolve runfiles path: {rlocation}")
    if not (path := Path(resolved)).exists():
        raise RuntimeError(f"Resolved path does not exist: {path}")
    return path


def find_path(rlocation: str) -> Path | None:
    """Resolve a runfiles path, or None when the running target did not package it.

    For an asset a library uses when its binary ships it and does without otherwise — the
    absence is a valid state of the caller's contract, not a failure to report.
    """
    resolved = _get_runfiles().Rlocation(rlocation)
    if not resolved:
        return None
    path = Path(resolved)
    return path if path.exists() else None
