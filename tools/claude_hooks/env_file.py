"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _exports_from_dict(env_vars: dict[str, str]) -> list[str]:
    """Generate export lines from a dict of env var name -> value.

    Properly shell-escapes values to handle special characters.
    """
    return [f"export {name}={shlex.quote(value)}" for name, value in env_vars.items()]


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

    # Upstream proxy (original proxy before hook rewrites)
    upstream_proxy_url: str | None

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

    # Bazel proxy configuration
    local_proxy = f"http://localhost:{vars.proxy_port}"
    combined_ca = str(vars.combined_ca)

    bazel_config = {
        "BAZEL_PROXY_PORT": str(vars.proxy_port),
        "BAZEL_LOCAL_PROXY": local_proxy,
        "BAZELISK_PATH": str(vars.bazelisk_path),
        "BAZEL_PROXY_BAZELRC": str(vars.bazel_proxy_rc),
        "BAZEL_REPO_ROOT": str(vars.repo_root),
    }
    ca_config = dict.fromkeys(
        ["NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"], combined_ca
    )
    exports.extend(["", "# Bazel proxy configuration"])
    exports.extend(_exports_from_dict(bazel_config | ca_config))

    # Proxy env vars for subprocesses (point to local auth-forwarding proxy)
    # These override Anthropic's original proxy vars so subprocesses use our local proxy
    proxy_var_names = [
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "GLOBAL_AGENT_HTTPS_PROXY",
        "GLOBAL_AGENT_HTTP_PROXY",
        "YARN_HTTPS_PROXY",
        "YARN_HTTP_PROXY",
    ]
    exports.extend(["", "# Proxy env vars for subprocesses (point to local auth-forwarding proxy)"])
    exports.extend(_exports_from_dict(dict.fromkeys(proxy_var_names, local_proxy)))

    # Upstream proxy (original before hook rewrites HTTPS_PROXY)
    # Exported so tests can chain through the real upstream when testing the hook
    if vars.upstream_proxy_url:
        exports.extend(["", "# Original upstream proxy (for tests that need to chain through)"])
        exports.extend(_exports_from_dict({"CLAUDE_HOOKS_UPSTREAM_PROXY_URL": vars.upstream_proxy_url}))

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
