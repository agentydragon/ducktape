"""BuildBuddy remote cache configuration."""

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildbuddyConfigured:
    """BuildBuddy was configured — session bazelrc was written."""

    bazelrc_path: Path


@dataclass(frozen=True)
class BuildbuddyNotConfigured:
    """BuildBuddy was not configured (no API key available)."""


type BuildbuddySetup = BuildbuddyConfigured | BuildbuddyNotConfigured


def setup_buildbuddy(*, api_key: str, session_dir: Path) -> BuildbuddyConfigured:
    """Write a per-session buildbuddy.bazelrc with the given API key."""
    session_bazelrc = session_dir / "buildbuddy.bazelrc"
    session_bazelrc.write_text(
        "# BuildBuddy authentication (auto-generated per session)\n"
        "# Static configuration is in .bazelrc under build:rbe\n"
        f"common --remote_header=x-buildbuddy-api-key={api_key}\n"
        "\n"
        "# Enable RBE (platforms, exec properties in .bazelrc + BUILD.bazel platform)\n"
        "build --config=rbe\n"
    )

    logger.info("BuildBuddy remote cache configured at %s", session_bazelrc)
    return BuildbuddyConfigured(bazelrc_path=session_bazelrc)
