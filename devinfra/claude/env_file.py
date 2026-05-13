"""Environment file generation for session hooks.

Centralizes all environment variable exports into a single file write.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from devinfra.claude.managed_files import write_config
from devinfra.claude.settings import ENV_SESSION_DIR, ENV_SUPERVISOR_PORT
from util.bazel.subprocess import exports_from_dict


def parse_env_null_delimited(raw: bytes) -> dict[str, str]:
    """Parse `env -0` (or `bash -c 'env -0'`) stdout into a dict.

    `env -0` writes NUL-delimited `KEY=VALUE` records. Values may contain
    newlines and `=` signs; only the first `=` is a separator. Empty records
    (e.g. trailing NUL) are skipped.
    """
    result: dict[str, str] = {}
    for item in raw.split(b"\x00"):
        if not item:
            continue
        key_b, _, val_b = item.partition(b"=")
        result[key_b.decode(errors="replace")] = val_b.decode(errors="replace")
    return result


# Runtime env var names (written by session hook, read by bazel_wrapper)
ENV_SESSION_BAZELRC = "SESSION_BAZELRC"


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

    All profiles set shims_dir (session bin/) and session_bazelrc. Other fields
    are populated based on profile flags (setup_docker, etc.).
    """

    # Required in all profiles
    shims_dir: Path
    session_bazelrc: Path

    # Per-session directory
    session_dir: Path | None = None

    # Supervisor port (when setup_docker is enabled)
    supervisor_port: int | None = None

    # Container runtime env vars (when setup_docker is enabled)
    docker_env: dict[str, str] | None = None

    # Session metadata timestamp
    hook_timestamp: datetime | None = None

    # bbr session bazelrc (metadata tags for BuildBuddy invocations)
    bbr_bazelrc: Path | None = None

    # Delta from startup_env_script: vars added/changed vs. os.environ (secrets, tokens, etc.)
    env_overlay: dict[str, str] = field(default_factory=dict)

    # Extra shell script content from config.yaml (appended verbatim)
    extra_env_script: str | None = None

    # System OpenJDK home, when one was found on the host. Exported as
    # JAVA_HOME so that `bb` (which doesn't read the session bazelrc) and any
    # other Java-launcher consumer pick up the same system JDK as Bazel's
    # server. On a system JDK, $JAVA_HOME/lib/security/cacerts is the OS-
    # managed truststore (Debian: symlink to /etc/ssl/certs/java/cacerts), so
    # HTTPS clients running under that JVM trust whatever CAs are in
    # /etc/ssl/certs/ca-certificates.crt — including a TLS-inspection CA.
    system_java_home: Path | None = None


def write_env_file(env_file: Path, vars: EnvVars) -> None:
    """Write environment variables to file.

    This is the SINGLE write point for all session environment variables.
    Works for both web mode (all fields set) and CLI mode (minimal fields).
    """
    path_str = os.pathsep.join(filter(None, [str(vars.shims_dir), os.environ.get("PATH")]))

    exports: list[str] = ["# Environment configured by session start hook"]
    if vars.hook_timestamp:
        exports.append(f"# Timestamp: {vars.hook_timestamp.isoformat()}")
    exports.extend(
        ["", "# Bazel tooling", *exports_from_dict({"PATH": path_str, ENV_SESSION_BAZELRC: vars.session_bazelrc})]
    )

    if vars.session_dir is not None:
        session_config: dict[str, str | Path] = {ENV_SESSION_DIR: vars.session_dir}
        if vars.supervisor_port is not None:
            session_config[ENV_SUPERVISOR_PORT] = str(vars.supervisor_port)
        exports.extend(["", "# Session configuration"])
        exports.extend(exports_from_dict(session_config))

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

    if vars.hook_timestamp:
        exports.extend(["", "# Session metadata"])
        exports.extend(exports_from_dict({"DUCKTAPE_SESSION_START_HOOK_TS": vars.hook_timestamp.isoformat()}))

    if vars.bbr_bazelrc:
        exports.extend(["", "# bbr session bazelrc (BuildBuddy invocation metadata)"])
        exports.extend(exports_from_dict({"BBR_BAZELRC": vars.bbr_bazelrc}))

    if vars.system_java_home is not None:
        # Export JAVA_HOME (and PATH-prefix its bin) so that consumers that
        # bypass the session bazelrc (notably `bb`, which does not source
        # <session_dir>/bazelrc) still pick up the system OpenJDK and its
        # OS-synced cacerts truststore. The session bazelrc separately wires
        # `startup --server_javabase=<system_java_home>` for the Bazel server.
        exports.extend(["", "# System OpenJDK with OS-managed cacerts (for bb and direct java callers)"])
        exports.extend(exports_from_dict({"JAVA_HOME": vars.system_java_home}))
        exports.append(f'export PATH="{vars.system_java_home}/bin:$PATH"')

    if vars.env_overlay:
        exports.extend(["", "# Secrets from startup_env_script"])
        exports.extend(exports_from_dict(vars.env_overlay))

    if vars.extra_env_script:
        exports.extend(["", "# Extra env script from config.yaml"])
        exports.append(vars.extra_env_script.rstrip())

    # Session bin dir must be first on PATH regardless of what env_overlay or
    # extra_env_script did to PATH above. Emit this last so it always wins.
    exports.extend(["", f'export PATH="{vars.shims_dir}:$PATH"'])

    content = "\n".join(exports) + "\n"
    write_config(env_file, content, "session environment")
