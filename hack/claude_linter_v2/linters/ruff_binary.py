"""Resolve the ruff binary path for subprocess invocation.

Ruff is a standalone Rust binary distributed via PyPI. It cannot be reliably
invoked via ``python -m ruff`` in Bazel sandboxes because the pip package
only provides a thin Python wrapper that shells out to a platform-specific
binary, and PYTHONPATH propagation alone doesn't guarantee the binary is
findable.

Resolution order:
1. ``RUFF_BIN`` env var — expected to be an rlocation path when running
   under Bazel (set via ``env`` in BUILD.bazel, resolved via runfiles).
2. ``shutil.which("ruff")`` — for non-Bazel usage (developer machines, CI).
"""

import logging
import os
import shutil
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

_RUFF_BIN_ENV = "RUFF_BIN"


def _resolve_rlocation(rlocation_path: str) -> str | None:
    """Resolve an rlocation path via Bazel runfiles."""
    try:
        # Lazy import: python.runfiles is optional (only available under Bazel)
        from python.runfiles import runfiles  # type: ignore[import-not-found]  # noqa: PLC0415

        r = runfiles.Create()
        if r is None:
            return None
        resolved: str | None = r.Rlocation(rlocation_path)
        if resolved and Path(resolved).exists():
            return resolved
    except ImportError:
        pass
    return None


@cache
def find_ruff_binary() -> str | None:
    """Find the ruff binary, returning its absolute path or None.

    Caches the result for the lifetime of the process.
    """
    # 1. RUFF_BIN env var (Bazel rlocation path or absolute path)
    if env_val := os.environ.get(_RUFF_BIN_ENV):
        # Try as rlocation first
        if resolved := _resolve_rlocation(env_val):
            logger.debug(f"Resolved ruff via RUFF_BIN rlocation: {resolved}")
            return resolved
        # Try as direct path
        if Path(env_val).exists():
            logger.debug(f"Resolved ruff via RUFF_BIN path: {env_val}")
            return env_val
        logger.warning(f"RUFF_BIN set to {env_val!r} but could not resolve")

    # 2. PATH lookup
    if which_path := shutil.which("ruff"):
        logger.debug(f"Found ruff on PATH: {which_path}")
        return which_path

    return None
