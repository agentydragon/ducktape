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


def _own_repo_prefix() -> str:
    """Canonical runfiles prefix for *this* (ducktape's) Bazel repository.

    `Runfiles.CurrentRepository()` resolves the Bazel repo that owns the calling
    `.py` file (via its frame's source path / repo-mapping). This function lives in
    `util/bazel/runfiles.py`, which is always compiled into the ducktape repo, so
    the lookup yields ducktape's *canonical* repo name regardless of who calls it:
    `""` when ducktape is the Bazel main repo, or `"ducktape+"` (the canonical
    `<module>+` name) when ducktape is consumed as an external module. The runfiles
    tree is keyed by canonical repo name, with the main repo's tree aliased to the
    `_main` workspace dir — hence the `_main` fallback for the empty (main) name.
    """
    repo = _get_runfiles().CurrentRepository()
    return repo if repo else "_main"


def get_required_own_repo_path(relpath: str) -> Path:
    """Resolve a runfiles path that lives in *this* repository, repo-agnostically.

    Use instead of a hardcoded `_main/`-prefixed `get_required_path` for data deps
    that ship in ducktape's own runfiles. A literal `_main/` prefix only resolves
    when ducktape is the Bazel main repo; an external consumer's runfiles place the
    same data under `ducktape+/...`, so the hardcoded lookup raises. This helper
    derives the correct prefix from the live runfiles repo-mapping instead.

    Args:
        relpath: Repo-root-relative path, e.g. "augur/api/testdata/config.yaml".
    """
    return get_required_path(f"{_own_repo_prefix()}/{relpath}")
