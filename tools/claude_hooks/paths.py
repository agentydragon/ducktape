"""Centralized path management for claude_hooks.

Single source of truth for all hook-related directories. Uses platformdirs
for proper cross-platform XDG Base Directory specification support.

Environment Variables (in priority order):
1. CLAUDE_HOOKS_*_DIR - Direct override for specific directory
2. XDG_CACHE_HOME / XDG_CONFIG_HOME - XDG standard directories (via platformdirs)
3. Platform defaults (Linux: ~/.cache, ~/.config; macOS: ~/Library/Caches, etc.)
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir


def get_cache_dir() -> Path:
    """Get base cache directory for claude-hooks.

    Uses platformdirs.user_cache_dir() which respects XDG_CACHE_HOME on Linux.
    """
    return Path(user_cache_dir(appname="claude-hooks", ensure_exists=False))


def get_supervisor_dir() -> Path:
    """Get supervisor configuration directory.

    Priority:
    1. CLAUDE_HOOKS_SUPERVISOR_DIR (test override)
    2. platformdirs.user_config_dir() / "supervisor"

    platformdirs respects XDG_CONFIG_HOME on Linux, uses proper platform defaults elsewhere.
    """
    if env_dir := os.environ.get("CLAUDE_HOOKS_SUPERVISOR_DIR"):
        return Path(env_dir)
    return Path(user_config_dir(appname="claude-hooks", ensure_exists=False)) / "supervisor"


def get_bazel_proxy_dir() -> Path:
    """Get bazel proxy cache directory.

    Priority:
    1. CLAUDE_HOOKS_BAZEL_PROXY_DIR (test override)
    2. platformdirs.user_cache_dir() / "bazel-proxy"

    platformdirs respects XDG_CACHE_HOME on Linux, uses proper platform defaults elsewhere.
    """
    if env_dir := os.environ.get("CLAUDE_HOOKS_BAZEL_PROXY_DIR"):
        return Path(env_dir)
    return get_cache_dir() / "bazel-proxy"


def get_podman_dir() -> Path:
    """Get podman configuration and storage directory.

    Priority:
    1. CLAUDE_HOOKS_PODMAN_DIR (test override)
    2. platformdirs.user_cache_dir() / "podman"

    Contains: storage.conf, containers.conf, registries.conf, storage/, runroot/
    """
    if env_dir := os.environ.get("CLAUDE_HOOKS_PODMAN_DIR"):
        return Path(env_dir)
    return get_cache_dir() / "podman"


def get_containers_config_dir() -> Path:
    """Get user-level containers config directory (~/.config/containers).

    Used for policy.json which has hardcoded lookup paths:
    1. $HOME/.config/containers/policy.json (user-level, we use this)
    2. /etc/containers/policy.json (system-level, we avoid)
    """
    return Path(user_config_dir(appname="containers", ensure_exists=False))
