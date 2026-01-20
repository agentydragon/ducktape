"""XDG-compliant paths for Claude Code hooks.

Uses platformdirs for proper XDG base directory resolution.
All functions support env var overrides for testing.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

# App name for platformdirs
_APP_NAME = "claude-code-web"
_APP_AUTHOR: bool | str = False  # No author subdirectory on Linux


def get_hook_cache_dir() -> Path:
    """Get main hook cache directory (XDG_CACHE_HOME/claude-code-web)."""
    return Path(platformdirs.user_cache_dir(_APP_NAME, _APP_AUTHOR))


def get_hook_log_file() -> Path:
    """Get session start log file path."""
    return get_hook_cache_dir() / "session-start.log"


def get_hook_timestamp_file() -> Path:
    """Get session hook last run timestamp file."""
    return get_hook_cache_dir() / "session-hook-last-run"


def get_bazel_proxy_dir() -> Path:
    """Get bazel proxy directory, allowing override via CLAUDE_HOOKS_BAZEL_PROXY_DIR."""
    if env_dir := os.environ.get("CLAUDE_HOOKS_BAZEL_PROXY_DIR"):
        return Path(env_dir)
    return Path(platformdirs.user_cache_dir("bazel-proxy", _APP_AUTHOR))


def get_supervisor_dir() -> Path:
    """Get supervisor directory, allowing override via CLAUDE_HOOKS_SUPERVISOR_DIR."""
    if env_dir := os.environ.get("CLAUDE_HOOKS_SUPERVISOR_DIR"):
        return Path(env_dir)
    return Path(platformdirs.user_config_dir("supervisor", _APP_AUTHOR))
