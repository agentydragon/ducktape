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


def own_repo_rlocation(relative: str) -> str:
    """A runfiles path for `relative` inside THIS repository, wherever it sits in the build.

    A runfiles path starts with a repository name, and `_main` names the ROOT module — right
    for a test or tool that only ever runs inside this repo, wrong for library code a dependent
    repo imports. There `_main` is the DEPENDENT's root and this repo is `ducktape+`, so a
    `_main/...` lookup either resolves to nothing or, worse, to the dependent's own file at
    that path. Library code reaching a data file it ships must go through here.
    """

    # Frame 1 is this function, which lives in this repository whoever called it — so the
    # canonical name comes out right without every caller having to pass its own.
    return f"{_get_runfiles().CurrentRepository() or '_main'}/{relative}"


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
