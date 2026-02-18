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

    TODO: Make this session-local. Instead of writing to the user-global
    ~/.config/bazel/buildbuddy.bazelrc and modifying ~/.bazelrc, generate the
    BuildBuddy bazelrc content directly and write it to session_dir/buildbuddy.bazelrc,
    then include it via an extra ``--bazelrc=`` flag injected by the wrapper (or by
    appending a ``try-import`` line to the session bazelrc template). That would keep
    BuildBuddy config fully within the session directory and avoid touching global Bazel
    config files.
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
