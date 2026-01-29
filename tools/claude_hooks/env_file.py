"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.settings import ENV_SUPERVISOR_PORT

# Runtime env var names (written by session hook, read by bazel_wrapper)
ENV_AUTH_PROXY_PORT = "AUTH_PROXY_PORT"
ENV_AUTH_PROXY_URL = "AUTH_PROXY_URL"
ENV_AUTH_PROXY_BAZELRC = "AUTH_PROXY_BAZELRC"
ENV_BAZELISK_PATH = "BAZELISK_PATH"
ENV_BAZEL_REPO_ROOT = "BAZEL_REPO_ROOT"


def _exports_from_dict(env_vars: Mapping[str, str | Path]) -> list[str]:
    """Generate export lines from a dict of env var name -> value.

    Properly shell-escapes values to handle special characters.
    Accepts both str and Path values.
    """
    return [f"export {name}={shlex.quote(str(value))}" for name, value in env_vars.items()]


@dataclass
class EnvVars:
    """Collected environment variables for session.

    All environment variables that need to be exported are collected
    throughout the session hook setup and written once at the end.
    """

    # Auth proxy and Bazel configuration
    proxy_port: int
    supervisor_port: int  # Needed by bazel_wrapper to connect to supervisor
    repo_root: Path
    combined_ca: Path
    bazel_wrapper_dir: Path
    bazelisk_path: Path
    auth_proxy_rc: Path

    # Nix paths
    nix_paths: list[Path]

    # Podman/Docker
    docker_host: str | None
    podman_env: dict[str, str] | None  # CONTAINERS_CONF, CONTAINERS_STORAGE_CONF, etc.

    # Session metadata
    hook_timestamp: datetime


def write_env_file(env_file: Path, vars: EnvVars) -> None:
    """Write environment variables to file.

    This is the SINGLE write point for all session environment variables.
    All env vars are collected during setup and written once at the end.

    Args:
        env_file: Path to environment file (CLAUDE_ENV_FILE)
        vars: Collected environment variables
    """
    exports = [
        "# Environment configured by session start hook",
        f"# Timestamp: {vars.hook_timestamp.isoformat()}",
        "",
        "# Bazel tooling",
        f'export PATH="{vars.bazel_wrapper_dir}:$PATH"',
    ]

    # Add nix to PATH
    if vars.nix_paths:
        nix_path_str = ":".join(str(p) for p in vars.nix_paths)
        exports.append(f'export PATH="{nix_path_str}:$PATH"')

    # Auth proxy configuration
    local_proxy = f"http://localhost:{vars.proxy_port}"

    auth_proxy_config: dict[str, str | Path] = {
        ENV_AUTH_PROXY_PORT: str(vars.proxy_port),
        ENV_AUTH_PROXY_URL: local_proxy,
        ENV_BAZELISK_PATH: vars.bazelisk_path,
        ENV_AUTH_PROXY_BAZELRC: vars.auth_proxy_rc,
        ENV_BAZEL_REPO_ROOT: vars.repo_root,
        # Supervisor port needed by bazel_wrapper to connect to supervisor
        ENV_SUPERVISOR_PORT: str(vars.supervisor_port),
    }
    ca_config: dict[str, str | Path] = dict.fromkeys(SSL_CA_ENV_VARS, vars.combined_ca)
    exports.extend(["", "# Auth proxy configuration"])
    exports.extend(_exports_from_dict(auth_proxy_config | ca_config))

    # NOTE: We intentionally do NOT export HTTPS_PROXY/HTTP_PROXY here.
    # Anthropic sets these in the container with fresh JWT credentials.
    # Only the bazel wrapper overrides them for its subprocess.
    # See README.md "Our Design Principle" section.

    # Docker/Podman configuration
    if vars.docker_host or vars.podman_env:
        exports.extend(["", "# Podman/Docker configuration"])
        if vars.docker_host:
            exports.extend(_exports_from_dict({"DOCKER_HOST": vars.docker_host}))
        if vars.podman_env:
            exports.extend(_exports_from_dict(vars.podman_env))

    # Session metadata
    exports.extend(["", "# Session metadata"])
    exports.extend(_exports_from_dict({"DUCKTAPE_SESSION_START_HOOK_TS": vars.hook_timestamp.isoformat()}))

    content = "\n".join(exports) + "\n"
    env_file.write_text(content)
