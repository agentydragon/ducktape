"""Specimens path derivation."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


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
