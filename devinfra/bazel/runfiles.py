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


def get_required_path(rlocation: str, *, source_repo: str | None = None) -> Path:
    """Resolve a runfiles path to an existing Path, raising if missing.

    ``source_repo`` is the canonical repository name whose mapping should be used for
    apparent repository names in ``rlocation``. When omitted, rules_python uses the mapping
    for this wrapper's repository. Pass it explicitly when ``rlocation`` contains an
    apparent name from a different repository's context. Raises when runfiles cannot be
    initialized, ``Rlocation`` has no result, or the resolved path does not exist.
    """
    rf = _get_runfiles()
    if not (resolved := rf.Rlocation(rlocation, source_repo=source_repo)):
        raise RuntimeError(f"Could not resolve runfiles path: {rlocation}")
    if not (path := Path(resolved)).exists():
        raise RuntimeError(f"Resolved path does not exist: {path}")
    return path


def find_path(rlocation: str, *, source_repo: str | None = None) -> Path | None:
    """Resolve a runfiles path, or None when the target did not package the asset.

    For an asset a library uses when its binary ships it and does without otherwise — the
    absence is a valid state of the caller's contract, not a failure to report. Returns None
    if Rlocation has no result or the resolved path does not exist. A missing Bazel runfiles
    environment still raises, as it does for ``get_required_path``.

    ``source_repo`` is the canonical repository name whose mapping should be used for
    apparent repository names in ``rlocation``. When omitted, rules_python uses the mapping
    for this wrapper's repository. Pass it explicitly for an apparent name from a different
    repository's context. This helper still raises if no Bazel runfiles environment exists.
    """
    rf = _get_runfiles()
    resolved = rf.Rlocation(rlocation, source_repo=source_repo)
    if not resolved:
        return None
    path = Path(resolved)
    return path if path.exists() else None
