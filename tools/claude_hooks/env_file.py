"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tools.claude_hooks.managed_files import write_config
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.settings import ENV_SUPERVISOR_PORT

# Runtime env var names (written by session hook, read by bazel_wrapper)
ENV_AUTH_PROXY_PORT = "AUTH_PROXY_PORT"
ENV_AUTH_PROXY_URL = "AUTH_PROXY_URL"
ENV_AUTH_PROXY_BAZELRC = "AUTH_PROXY_BAZELRC"
ENV_BAZELISK_PATH = "BAZELISK_PATH"
ENV_BAZEL_REPO_ROOT = "BAZEL_REPO_ROOT"


# NO_PROXY entries that break Go module downloads in the gVisor sandbox.
# proxy.golang.org redirects zip downloads to storage.googleapis.com;
# if that's in NO_PROXY, Go bypasses the egress proxy and DNS fails.
_NO_PROXY_STRIP_PATTERNS = {"*.googleapis.com", "*.google.com"}


def _strip_no_proxy_google() -> dict[str, str] | None:
    """Strip *.googleapis.com and *.google.com from NO_PROXY/no_proxy.

    Returns dict of env var overrides, or None if no change needed.
    """
    original = os.environ.get("NO_PROXY", "")
    if not original:
        return None

    entries = [e.strip() for e in original.split(",")]
    filtered = [e for e in entries if e not in _NO_PROXY_STRIP_PATTERNS]

    if len(filtered) == len(entries):
        return None  # Nothing to strip

    cleaned = ",".join(filtered)
    return {"NO_PROXY": cleaned, "no_proxy": cleaned}


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

    # Fix NO_PROXY: Anthropic's container sets NO_PROXY with *.googleapis.com
    # and *.google.com, which breaks Go module downloads. The Go module proxy
    # (proxy.golang.org) redirects zip downloads to storage.googleapis.com;
    # Go's net/http honors NO_PROXY, bypasses the egress proxy for that domain,
    # and DNS fails (no direct internet in gVisor sandbox). Strip these entries
    # so all external traffic goes through the proxy.
    no_proxy_override = _strip_no_proxy_google()
    if no_proxy_override is not None:
        exports.extend(["", "# NO_PROXY fix: strip *.googleapis.com/*.google.com (breaks Go module downloads)"])
        exports.extend(_exports_from_dict(no_proxy_override))

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
    write_config(env_file, content, "session environment")


def write_direnv_env_file(env_file_path: Path, env_vars: dict[str, str]) -> None:
    """Write direnv-exported environment variables to CLAUDE_ENV_FILE.

    This is the CLI-mode counterpart to write_env_file. Both routes go
    through write_config so the env file is always canary-protected.
    """
    lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(env_vars.items())]
    content = "\n".join(lines) + "\n"
    write_config(env_file_path, content, "direnv environment")
