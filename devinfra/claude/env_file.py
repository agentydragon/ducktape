"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS
from devinfra.claude.managed_files import write_config
from devinfra.claude.settings import ENV_SESSION_DIR, ENV_SUPERVISOR_PORT
from util.bazel.subprocess import exports_from_dict

# Runtime env var names (written by session hook, read by bazel_wrapper)
ENV_AUTH_PROXY_PORT = "AUTH_PROXY_PORT"
ENV_AUTH_PROXY_URL = "AUTH_PROXY_URL"
ENV_SESSION_BAZELRC = "SESSION_BAZELRC"
ENV_BAZELISK_PATH = "BAZELISK_PATH"


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


@dataclass
class EnvVars:
    """Collected environment variables for session.

    Used in both web and CLI modes. Web mode sets all fields; CLI mode
    sets only bazel_wrapper_dir, session_bazelrc, and with_direnv.
    """

    # Required in all modes
    bazel_wrapper_dir: Path
    session_bazelrc: Path

    # Web mode: per-session directory
    session_dir: Path | None = None

    # Web mode: auth proxy and Bazel configuration
    proxy_port: int | None = None
    supervisor_port: int | None = None
    combined_ca: Path | None = None
    bazelisk_path: Path | None = None

    # Container runtime env vars (web mode)
    docker_env: dict[str, str] | None = None

    # Session metadata timestamp (web mode)
    hook_timestamp: datetime | None = None

    # mkcert localhost TLS certificate (web mode)
    mkcert_cert: Path | None = None
    mkcert_key: Path | None = None

    # K8s secrets env vars (web mode)
    secrets_env_vars: dict[str, str] | None = None

    # CLI mode: include direnv eval for .envrc propagation
    with_direnv: bool = False

    # Extra shell script content from config.yaml (appended verbatim)
    extra_env_script: str | None = None


def write_env_file(env_file: Path, vars: EnvVars) -> None:
    """Write environment variables to file.

    This is the SINGLE write point for all session environment variables.
    Works for both web mode (all fields set) and CLI mode (minimal fields).
    """
    path_str = os.pathsep.join(filter(None, [str(vars.bazel_wrapper_dir), os.environ.get("PATH")]))

    exports: list[str] = ["# Environment configured by session start hook"]
    if vars.hook_timestamp:
        exports.append(f"# Timestamp: {vars.hook_timestamp.isoformat()}")
    exports.extend(
        ["", "# Bazel tooling", *exports_from_dict({"PATH": path_str, ENV_SESSION_BAZELRC: vars.session_bazelrc})]
    )

    if vars.proxy_port is not None:
        assert vars.session_dir is not None
        assert vars.supervisor_port is not None
        assert vars.combined_ca is not None
        local_proxy = f"http://localhost:{vars.proxy_port}"
        auth_proxy_config: dict[str, str | Path] = {
            ENV_SESSION_DIR: vars.session_dir,
            ENV_AUTH_PROXY_PORT: str(vars.proxy_port),
            ENV_AUTH_PROXY_URL: local_proxy,
            # Supervisor port needed by bazel_wrapper to connect to supervisor
            ENV_SUPERVISOR_PORT: str(vars.supervisor_port),
        }
        ca_config: dict[str, str | Path] = dict.fromkeys(SSL_CA_ENV_VARS, vars.combined_ca)
        exports.extend(["", "# Auth proxy configuration"])
        exports.extend(exports_from_dict(auth_proxy_config | ca_config))
    elif vars.session_dir is not None:
        # UDS mode: no TCP proxy port, but session dir and CA still needed
        uds_config: dict[str, str | Path] = {ENV_SESSION_DIR: vars.session_dir}
        if vars.supervisor_port is not None:
            uds_config[ENV_SUPERVISOR_PORT] = str(vars.supervisor_port)
        if vars.combined_ca is not None:
            ca_config = dict.fromkeys(SSL_CA_ENV_VARS, vars.combined_ca)
            uds_config.update(ca_config)
        exports.extend(["", "# Session configuration (UDS proxy mode)"])
        exports.extend(exports_from_dict(uds_config))

    # Bazelisk path (always set when available, regardless of proxy mode)
    if vars.bazelisk_path is not None:
        exports.extend(["", "# Bazelisk path"])
        exports.extend(exports_from_dict({ENV_BAZELISK_PATH: vars.bazelisk_path}))

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
        exports.extend(exports_from_dict(no_proxy_override))

    if vars.docker_env:
        exports.extend(["", "# Docker/Podman configuration"])
        exports.extend(exports_from_dict(vars.docker_env))

    if vars.mkcert_cert and vars.mkcert_key:
        exports.extend(["", "# Localhost TLS certificate (mkcert)"])
        exports.extend(exports_from_dict({"MKCERT_CERT": vars.mkcert_cert, "MKCERT_KEY": vars.mkcert_key}))

    if vars.hook_timestamp:
        exports.extend(["", "# Session metadata"])
        exports.extend(exports_from_dict({"DUCKTAPE_SESSION_START_HOOK_TS": vars.hook_timestamp.isoformat()}))

    if vars.secrets_env_vars:
        exports.extend(["", "# K8s cluster secrets"])
        exports.extend(exports_from_dict(vars.secrets_env_vars))

    # Point Ansible's local tmp to a sandbox-writable directory so that
    # pre-commit ansible-syntax-check works when /tmp is read-only in the
    # Claude Code sandbox. Use $TMPDIR at runtime (set by sandbox to a
    # writable path like /tmp/claude), not the hook daemon's TMPDIR which
    # resolves to bare /tmp.
    # Raw shell expression — not passed through exports_from_dict because
    # shlex.quote would single-quote the $TMPDIR variable expansion.
    exports.extend(["", "# Ansible (pre-commit sandbox compatibility)"])
    exports.append('export ANSIBLE_LOCAL_TEMP="${TMPDIR:-/tmp}/ansible-tmp"')

    if vars.with_direnv and shutil.which("direnv"):
        exports.append('eval "$(direnv export bash 2>/dev/null)"')

    if vars.extra_env_script:
        exports.extend(["", "# Extra env script from config.yaml"])
        exports.append(vars.extra_env_script.rstrip())

    content = "\n".join(exports) + "\n"
    write_config(env_file, content, "session environment")
