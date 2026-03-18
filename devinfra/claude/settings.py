"""Centralized configuration for claude using Pydantic Settings.

Hook-related configuration (feature flags, port overrides, k8s token).
Path computations live in session_paths.py.

Environment Variables (in priority order):
1. DUCKTAPE_CLAUDE_HOOKS_* - Direct override for specific setting
2. XDG_CACHE_HOME / XDG_CONFIG_HOME - XDG standard directories (via platformdirs)
3. Platform defaults (Linux: ~/.cache, ~/.config; macOS: ~/Library/Caches, etc.)
"""

import importlib.resources
import os
from importlib.resources.abc import Traversable
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Config files bundled with the package (infrastructure config: bazelrc, env, podman)
CONFIG_FILES: Traversable = importlib.resources.files("devinfra.claude.config")

# TODO: Rename prefix to DUCKTAPE_CLAUDE_ to match new dir name.
# Environment variable prefix (matches model_config.env_prefix)
ENV_PREFIX = "DUCKTAPE_CLAUDE_HOOKS_"


def _env_name(field: str) -> str:
    """Compute env var name from field name. Pattern: ENV_PREFIX + field.upper()"""
    return f"{ENV_PREFIX}{field.upper()}"


# Environment variable names (used by tests and env_file.py)
ENV_SUPERVISOR_PORT = _env_name("supervisor_port")
ENV_AUTH_PROXY_PORT = _env_name("auth_proxy_port")
ENV_INSTALL_BAZELISK = _env_name("install_bazelisk")
ENV_CONTAINER_RUNTIME = _env_name("container_runtime")
ENV_SYSTEM_BAZEL = _env_name("system_bazel")
ENV_USE_WHEEL = _env_name("use_wheel")
ENV_SESSION_DIR = _env_name("session_dir")


def is_web_mode() -> bool:
    """Check if running in Claude Code web mode (CLAUDE_CODE_REMOTE=true)."""
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


class HookSettings(BaseSettings):
    """Configuration for claude via environment variables.

    Feature flags, port overrides, and k8s token. Path computations
    are in SessionPaths (session_paths.py).
    """

    model_config = SettingsConfigDict(env_prefix="DUCKTAPE_CLAUDE_HOOKS_", env_file_encoding="utf-8")

    # Port overrides (used by tests for free-port isolation)
    supervisor_port: int = Field(default=19001, description="Supervisor TCP port")
    auth_proxy_port: int = Field(default=18081, description="Auth proxy port")

    # Feature flags (enable/disable installations)
    install_bazelisk: bool = Field(default=True, description="Download and install bazelisk")
    install_mkcert: bool = Field(default=True, description="Install mkcert and generate localhost TLS cert")
    container_runtime: Literal["podman", "docker", "none"] = Field(
        default="docker", description="Container runtime to set up (podman, docker, or none)"
    )
    system_bazel: str | None = Field(
        default=None, description="Path to system bazel (used when install_bazelisk=False)"
    )

    k8s_token: str | None = Field(default=None, description="K8s SA token for reading secrets from cluster")

    # Test configuration
    use_wheel: bool = Field(default=False, description="Use installed wheel instead of source")

    # Per-session output directory. Exported as DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR so
    # subprocesses (e.g. bazel_wrapper) pick it up automatically via pydantic-settings.
    # Baked into the bazel/bazelisk shell wrapper at install time so it survives
    # into pre-commit and other subprocess invocations that don't source the env file.
    session_dir: str | None = Field(default=None, description="Per-session output directory (for bazel_wrapper env)")
