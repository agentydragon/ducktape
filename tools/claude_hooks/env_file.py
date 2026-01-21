"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class EnvVars:
    """Collected environment variables for session.

    All environment variables that need to be exported are collected
    throughout the session hook setup and written once at the end.
    """

    # Bazel configuration
    proxy_port: int
    repo_root: Path
    combined_ca: Path
    bazel_wrapper_dir: Path
    bazelisk_path: Path
    bazel_proxy_rc: Path

    # Nix paths
    nix_paths: list[Path]

    # Podman/Docker
    docker_host: str | None

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

    # Bazel proxy configuration
    local_proxy = f"http://localhost:{vars.proxy_port}"
    exports.extend(
        [
            "",
            "# Bazel proxy configuration",
            f"export BAZEL_PROXY_PORT={vars.proxy_port}",
            f'export BAZEL_LOCAL_PROXY="{local_proxy}"',
            f'export BAZELISK_PATH="{vars.bazelisk_path}"',
            f'export BAZEL_PROXY_BAZELRC="{vars.bazel_proxy_rc}"',
            f'export BAZEL_REPO_ROOT="{vars.repo_root}"',
            f'export NODE_EXTRA_CA_CERTS="{vars.combined_ca}"',
            f'export REQUESTS_CA_BUNDLE="{vars.combined_ca}"',
            f'export CURL_CA_BUNDLE="{vars.combined_ca}"',
            f'export SSL_CERT_FILE="{vars.combined_ca}"',
        ]
    )

    # Docker/Podman configuration
    if vars.docker_host:
        exports.extend(["", "# Podman/Docker configuration", f'export DOCKER_HOST="{vars.docker_host}"'])

    # Session metadata
    exports.extend(
        ["", "# Session metadata", f'export DUCKTAPE_SESSION_START_HOOK_TS="{vars.hook_timestamp.isoformat()}"']
    )

    content = "\n".join(exports) + "\n"
    env_file.write_text(content)
