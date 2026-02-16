"""BuildBuddy remote cache configuration."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BuildbuddySetup:
    """Result of BuildBuddy configuration."""

    configured: bool


def setup_buildbuddy(project_dir: Path, *, api_key: str | None = None) -> BuildbuddySetup:
    """Configure BuildBuddy remote cache.

    Writes config to ~/.config/bazel/buildbuddy.bazelrc and ensures
    ~/.bazelrc has the try-import line.
    """
    if not api_key:
        logger.info("BuildBuddy API key not provided, skipping setup")
        return BuildbuddySetup(configured=False)

    script_path = project_dir / "tools" / "setup_buildbuddy.sh"
    if not script_path.exists():
        logger.warning("BuildBuddy setup script not found: %s", script_path)
        return BuildbuddySetup(configured=False)

    result = subprocess.run(
        [script_path],
        env={**os.environ, "BUILDBUDDY_API_KEY": api_key},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 0:
        logger.info("BuildBuddy remote cache configured")
        return BuildbuddySetup(configured=True)
    logger.warning("BuildBuddy setup failed: %s", result.stderr)
    return BuildbuddySetup(configured=False)
