"""Centralized configuration for claude using Pydantic Settings.

Hook-related configuration (feature flags, port overrides, k8s token).
Path computations live in session_paths.py.

Environment Variables (in priority order):
1. DUCKTAPE_CLAUDE_HOOKS_* - Direct override for specific setting
2. XDG_CACHE_HOME / XDG_CONFIG_HOME - XDG standard directories (via platformdirs)
3. Platform defaults (Linux: ~/.cache, ~/.config; macOS: ~/Library/Caches, etc.)
"""

import importlib.resources
from importlib.resources.abc import Traversable

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Config files bundled with the package (infrastructure config: bazelrc, env)
CONFIG_FILES: Traversable = importlib.resources.files("devinfra.claude.config")

# TODO: Rename prefix to DUCKTAPE_CLAUDE_ to match new dir name.
# Environment variable prefix (matches model_config.env_prefix)
ENV_PREFIX = "DUCKTAPE_CLAUDE_HOOKS_"


def _env_name(field: str) -> str:
    """Compute env var name from field name. Pattern: ENV_PREFIX + field.upper()"""
    return f"{ENV_PREFIX}{field.upper()}"


# Environment variable names (used by tests and env_file.py)
ENV_SUPERVISOR_PORT = _env_name("supervisor_port")
ENV_SESSION_DIR = _env_name("session_dir")


class HookSettings(BaseSettings):
    """Configuration for claude via environment variables.

    Feature flags, port overrides, and k8s token. Path computations
    are in SessionPaths (session_paths.py).
    """

    model_config = SettingsConfigDict(env_prefix="DUCKTAPE_CLAUDE_HOOKS_", env_file_encoding="utf-8")

    # Port overrides (used by tests for free-port isolation)
    supervisor_port: int = Field(default=19001, description="Supervisor TCP port")

    # Profile YAML file path (repo-relative, e.g. devinfra/claude/hook_daemon/profiles/cli/profile.yaml)
    profile: str | None = Field(default=None, description="Profile YAML file path (repo-relative)")

    # Per-session output directory. Exported as DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR so
    # subprocesses (e.g. bazel_wrapper) pick it up automatically via pydantic-settings.
    # Baked into the bazel/bazelisk shell wrapper at install time so it survives
    # into pre-commit and other subprocess invocations that don't source the env file.
    session_dir: str | None = Field(default=None, description="Per-session output directory (for bazel_wrapper env)")
