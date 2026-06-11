"""Shared runfiles utilities for cluster validation scripts."""

from __future__ import annotations

import os
from pathlib import Path

from python.runfiles import runfiles

_RUNFILES_OPT = runfiles.Create()
if _RUNFILES_OPT is None:
    raise RuntimeError("Could not create runfiles")
RUNFILES: runfiles.Runfiles = _RUNFILES_OPT


def resolve_path(rlocation: str) -> Path:
    """Resolve a runfiles path to an absolute path."""
    resolved = RUNFILES.Rlocation(rlocation)
    if not resolved:
        raise RuntimeError(f"Could not resolve runfiles path: {rlocation}")
    path = Path(resolved)
    if not path.exists():
        raise RuntimeError(f"Resolved path does not exist: {path}")
    return path


def resolve_from_env(env_var: str) -> Path:
    """Resolve a binary path from an environment variable containing an rlocationpath.

    Use this for binaries with platform-specific repo names (like tf_toolchains).
    """
    rlocation_path = os.environ.get(env_var)
    if not rlocation_path:
        raise RuntimeError(f"{env_var} not set - run via Bazel")
    return resolve_path(rlocation_path)
