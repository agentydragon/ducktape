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
from enum import StrEnum
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
ENV_AUTH_PROXY_PORT = _env_name("auth_proxy_port")
ENV_SETUP_DOCKER = _env_name("setup_docker")
ENV_SESSION_DIR = _env_name("session_dir")


def is_web_mode() -> bool:
    """Check if running in Claude Code web mode (CLAUDE_CODE_REMOTE=true)."""
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


class ProxyMode(StrEnum):
    """Bazel proxy routing mode."""

    UDS = "uds"
    TCP = "tcp"


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
    install_mkcert: bool = Field(default=True, description="Install mkcert and generate localhost TLS cert")
    install_apt_packages: bool = True
    setup_docker: bool = Field(default=True, description="Set up Docker daemon under supervisor")

    k8s_token: str | None = Field(default=None, description="K8s SA token for reading secrets from cluster")

    warmup_bazel_server: bool = Field(default=True, description="Start Bazel server in background after session setup")

    proxy_mode: ProxyMode = Field(
        default=ProxyMode.UDS,
        description=(
            "Bazel proxy mode. 'uds': route gRPC via --remote_proxy/--bes_proxy UDS, "
            "BCR uses native JAVA_TOOL_OPTIONS proxy. 'tcp': route all Bazel traffic "
            "through localhost TCP HTTP CONNECT proxy (legacy)."
        ),
    )
    remote_proxy_target: str = Field(
        default="remote.buildbuddy.io:443",
        description="host:port for UDS remote proxy CONNECT tunnel (Bazel --remote_proxy destination)",
    )

    # Per-session output directory. Exported as DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR so
    # subprocesses (e.g. bazel_wrapper) pick it up automatically via pydantic-settings.
    # Baked into the bazel/bazelisk shell wrapper at install time so it survives
    # into pre-commit and other subprocess invocations that don't source the env file.
    session_dir: str | None = Field(default=None, description="Per-session output directory (for bazel_wrapper env)")
