"""Centralized configuration for claude_hooks using Pydantic Settings.

Single source of truth for all hook-related configuration. Uses pydantic-settings
for type-safe environment variable parsing with the DUCKTAPE_CLAUDE_HOOKS_ prefix.

Environment Variables (in priority order):
1. DUCKTAPE_CLAUDE_HOOKS_* - Direct override for specific setting
2. XDG_CACHE_HOME / XDG_CONFIG_HOME - XDG standard directories (via platformdirs)
3. Platform defaults (Linux: ~/.cache, ~/.config; macOS: ~/Library/Caches, etc.)
"""

from __future__ import annotations

import importlib.resources
from importlib.resources.abc import Traversable
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Config files bundled with the package (templates, etc.)
CONFIG_FILES: Traversable = importlib.resources.files("tools.claude_hooks.config")

# Environment variable prefix (matches model_config.env_prefix)
ENV_PREFIX = "DUCKTAPE_CLAUDE_HOOKS_"


def _env_name(field: str) -> str:
    """Compute env var name from field name. Pattern: ENV_PREFIX + field.upper()"""
    return f"{ENV_PREFIX}{field.upper()}"


# Environment variable names (used by tests and env_file.py)
# These are computed from field names to stay in sync with pydantic-settings
ENV_SUPERVISOR_DIR = _env_name("supervisor_dir")
ENV_SUPERVISOR_PORT = _env_name("supervisor_port")
ENV_AUTH_PROXY_DIR = _env_name("auth_proxy_dir")
ENV_AUTH_PROXY_PORT = _env_name("auth_proxy_port")
ENV_PODMAN_DIR = _env_name("podman_dir")
ENV_PODMAN_SOCKET = _env_name("podman_socket")
ENV_SKIP_BAZELISK = _env_name("skip_bazelisk")
ENV_SKIP_NIX = _env_name("skip_nix")
ENV_SKIP_PODMAN = _env_name("skip_podman")
ENV_SYSTEM_BAZEL = _env_name("system_bazel")
ENV_USE_WHEEL = _env_name("use_wheel")


class HookSettings(BaseSettings):
    """Configuration for claude_hooks via environment variables.

    All settings can be overridden with DUCKTAPE_CLAUDE_HOOKS_* environment variables.
    For example, DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT=9001 overrides supervisor_port.

    Usage:
        settings = HookSettings()
        supervisor_dir = settings.get_supervisor_dir()
    """

    model_config = SettingsConfigDict(env_prefix="DUCKTAPE_CLAUDE_HOOKS_", env_file_encoding="utf-8")

    # Directory overrides (test isolation)
    supervisor_dir: Path | None = Field(default=None, description="Override supervisor config directory")
    auth_proxy_dir: Path | None = Field(default=None, description="Override auth proxy cache directory")
    podman_dir: Path | None = Field(default=None, description="Override podman config directory")
    podman_socket: Path | None = Field(default=None, description="Override podman socket path")

    # Port overrides
    supervisor_port: int | None = Field(default=None, description="Override supervisor TCP port")
    auth_proxy_port: int | None = Field(default=None, description="Override auth proxy port")

    # Feature flags (skip installations for testing)
    skip_bazelisk: bool = Field(default=False, description="Skip bazelisk download (use system bazel)")
    skip_nix: bool = Field(default=False, description="Skip nix installation")
    skip_podman: bool = Field(default=False, description="Skip podman setup")
    system_bazel: Path | None = Field(default=None, description="Path to system bazel (used when skip_bazelisk=True)")

    # Test configuration
    use_wheel: bool = Field(default=False, description="Use installed wheel instead of source")

    def get_cache_dir(self) -> Path:
        """Get base cache directory for claude-hooks (auto-created)."""
        return Path(user_cache_dir(appname="claude-hooks", ensure_exists=True))

    def get_config_dir(self) -> Path:
        """Get base config directory for claude-hooks (auto-created)."""
        return Path(user_config_dir(appname="claude-hooks", ensure_exists=True))

    def get_supervisor_dir(self) -> Path:
        """Get supervisor configuration directory."""
        if self.supervisor_dir is not None:
            return self.supervisor_dir
        return self.get_config_dir() / "supervisor"

    def get_supervisor_pidfile(self) -> Path:
        """Get supervisor pidfile path."""
        return self.get_supervisor_dir() / "supervisord.pid"

    def get_supervisor_port(self) -> int:
        """Get supervisor port with default."""
        return self.supervisor_port if self.supervisor_port is not None else 19001

    def get_auth_proxy_dir(self) -> Path:
        """Get auth proxy cache directory."""
        if self.auth_proxy_dir is not None:
            return self.auth_proxy_dir
        return self.get_cache_dir() / "auth-proxy"

    def get_auth_proxy_port(self) -> int:
        """Get auth proxy port with default."""
        return self.auth_proxy_port if self.auth_proxy_port is not None else 18081

    def get_podman_dir(self) -> Path:
        """Get podman configuration and storage directory."""
        if self.podman_dir is not None:
            return self.podman_dir
        return self.get_cache_dir() / "podman"

    def get_containers_config_dir(self) -> Path:
        """Get user-level containers config directory (~/.config/containers).

        Used for policy.json which has hardcoded lookup paths:
        1. $HOME/.config/containers/policy.json (user-level, we use this)
        2. /etc/containers/policy.json (system-level, we avoid)
        """
        return Path(user_config_dir(appname="containers", ensure_exists=False))

    # Auth proxy file paths (centralized to avoid duplication)
    def get_auth_proxy_combined_ca(self) -> Path:
        """Get path to combined CA bundle (system CAs + proxy CA)."""
        return self.get_auth_proxy_dir() / "combined_ca.pem"

    def get_auth_proxy_rc(self) -> Path:
        """Get path to auth proxy bazelrc file."""
        return self.get_auth_proxy_dir() / "bazelrc"

    def get_auth_proxy_creds_file(self) -> Path:
        """Get path to upstream proxy credentials file."""
        return self.get_auth_proxy_dir() / "upstream_proxy"

    def get_auth_proxy_ca_file(self) -> Path:
        """Get path to extracted Anthropic CA file."""
        return self.get_auth_proxy_dir() / "anthropic_ca.pem"

    def get_auth_proxy_truststore(self) -> Path:
        """Get path to Java truststore with proxy CA."""
        return self.get_auth_proxy_dir() / "cacerts.jks"

    def get_bazelisk_path(self) -> Path:
        """Get the bazelisk binary path."""
        return self.get_auth_proxy_dir() / "bazelisk"

    def get_wrapper_dir(self) -> Path:
        """Get the wrapper directory (added to PATH)."""
        return self.get_auth_proxy_dir() / "bin"

    def get_wrapper_path(self) -> Path:
        """Get the wrapper script path."""
        return self.get_wrapper_dir() / "bazel"
