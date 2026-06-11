"""Resolve tool binaries from Bazel runfiles or PATH.

TODO: Consolidate to PATH-only resolution once all callers run in the Nix
devshell (which provides kustomize, flux, helm). Remove the runfiles fallback
and the util.bazel.runfiles dependency.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def resolve_tool(name: str, runfiles_rlocation: str) -> Path:
    """Find a tool binary via Bazel runfiles, falling back to PATH.

    Used by cluster validation scripts that run both under Bazel (tests) and
    from Nix-installed wheels (pre-commit hooks).
    """
    try:
        from util.bazel.runfiles import get_required_path  # noqa: PLC0415 — not available outside Bazel

        return get_required_path(runfiles_rlocation)
    except (ImportError, RuntimeError):
        pass
    if path := shutil.which(name):
        return Path(path)
    raise FileNotFoundError(f"{name} not found on PATH or in Bazel runfiles ({runfiles_rlocation})")
