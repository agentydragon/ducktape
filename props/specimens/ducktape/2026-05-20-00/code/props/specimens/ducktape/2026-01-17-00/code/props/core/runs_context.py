"""Centralized runs directory context and path derivation.

This module provides RunsContext for managing runs directory paths and a standard
timestamp formatting function.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def pkg_dir() -> Path:
    """Root directory of this package resources."""
    return Path(__file__).parent


def specimens_definitions_root() -> Path:
    """Directory with specimen definitions.

    Expects manifest.yaml files in {repo}/{version}/ subdirectories.

    Requires ADGN_PROPS_SPECIMENS_ROOT environment variable to be set.

    Returns:
        Path to specimens directory (guaranteed to exist with snapshot definitions)

    Raises:
        ValueError: If ADGN_PROPS_SPECIMENS_ROOT is not set
        FileNotFoundError: If specimens directory doesn't exist or has no manifest.yaml files
    """
    env_path = os.environ.get("ADGN_PROPS_SPECIMENS_ROOT")

    if not env_path:
        raise ValueError(
            "ADGN_PROPS_SPECIMENS_ROOT environment variable not set. "
            "Run from devenv shell (direnv allow) or set the variable manually."
        )

    specimens_root = Path(env_path).resolve()
    logger.debug(f"Using specimens root from ADGN_PROPS_SPECIMENS_ROOT: {specimens_root}")

    if not specimens_root.exists():
        raise FileNotFoundError(f"Specimens directory not found: {specimens_root}")

    if not any(specimens_root.rglob("manifest.yaml")):
        raise FileNotFoundError(
            f"No manifest.yaml files found in {specimens_root}. "
            f"Expected manifest.yaml files in <repo>/<version>/ subdirectories."
        )

    return specimens_root


def format_timestamp_session(dt: datetime | None = None) -> str:
    """Standard timestamp format for session/output directories: YYYYMMDD_HHMMSS.

    Args:
        dt: datetime to format (defaults to now)

    Returns:
        Formatted timestamp string (e.g., "20250127_153045")
    """
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S")


class RunsContext:
    """Context object for runs directory path derivation.

    Injected at CLI/entry point level. All path construction goes through this object.
    No code should independently compute runs paths or use hardcoded path tokens.
    """

    def __init__(self, base_dir: Path):
        """Initialize runs context.

        Args:
            base_dir: Base runs directory (e.g., pkg_dir() / "runs")
        """
        self.base_dir = base_dir

    @classmethod
    def from_pkg_dir(cls) -> RunsContext:
        """Create RunsContext from package directory (default location).

        Returns:
            RunsContext for pkg_dir() / "runs"
        """
        return cls(pkg_dir() / "runs")

    def issue_eval_dir(self, identifier: str, timestamp: str | None = None) -> Path:
        """Get output directory for issue evaluation runs (lint_issue harness).

        Args:
            identifier: Identifier for the eval (e.g., snapshot_issue_id or "all")
            timestamp: Optional timestamp string (defaults to creating new one)

        Returns:
            Path to eval output directory (created if it doesn't exist)
            Structure: runs/evals/{identifier}_{timestamp}/
        """
        if timestamp is None:
            timestamp = format_timestamp_session()
        path = self.base_dir / "evals" / f"{identifier}_{timestamp}"
        path.mkdir(parents=True, exist_ok=True)
        return path
