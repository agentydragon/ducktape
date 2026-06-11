"""Utilities for Bazel-run tools."""

from __future__ import annotations

import os
from pathlib import Path


def get_workspace_root() -> Path:
    """Get the workspace root directory.

    When run via 'bazel run', uses BUILD_WORKING_DIRECTORY env var.
    Otherwise falls back to current working directory.

    This is needed because 'bazel run' executes from the exec root,
    not the original working directory. Bazel sets BUILD_WORKING_DIRECTORY
    to the directory where 'bazel run' was invoked.
    """
    if build_wd := os.environ.get("BUILD_WORKING_DIRECTORY"):
        return Path(build_wd)
    return Path.cwd()
