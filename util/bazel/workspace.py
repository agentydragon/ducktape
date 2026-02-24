"""Bazel workspace environment variables.

``bazel run`` sets two env vars that let tools find the source tree:

- ``BUILD_WORKSPACE_DIRECTORY`` — the Bazel workspace root (repo root)
- ``BUILD_WORKING_DIRECTORY`` — the cwd where ``bazel run`` was invoked

Both fall back to ``Path.cwd()`` when not running under ``bazel run``.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_build_workspace_directory() -> Path:
    """Bazel workspace root (repo root). Falls back to cwd outside ``bazel run``."""
    if workspace := os.environ.get("BUILD_WORKSPACE_DIRECTORY"):
        return Path(workspace)
    return Path.cwd()


def get_build_working_directory() -> Path:
    """Directory where ``bazel run`` was invoked. Falls back to cwd outside ``bazel run``."""
    if build_wd := os.environ.get("BUILD_WORKING_DIRECTORY"):
        return Path(build_wd)
    return Path.cwd()
