"""`claude-hook write-kubeconfig <path>` — materialize a service-account kubeconfig.

Shared kubeconfig writer used by both `web_env.sh` (writes `~/.kube/config` at
daemon startup) and `claude-sandbox-kubectl-mcp.sh` (writes a tempfile for the
kubectl MCP server). Reuses the same `build_kubeconfig` helper the daemon uses
for its own session kubeconfig, so all three consumers agree on CA and proxy
handling.

Used to be a duplicated bash script at `devinfra/claude/kube_from_sops.sh` that
silently drifted — emitting a kubeconfig with no `certificate-authority-data`
and no `proxy-url`, breaking kubectl on Claude Code web (TLS inspecting proxy
+ MITM CA). This module is the single source of truth now.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

from devinfra.claude.auth_proxy.vars import get_proxy_url
from devinfra.claude.hook_daemon.config import ProfileConfig
from devinfra.claude.hook_daemon.kubeconfig import build_kubeconfig

logger = logging.getLogger(__name__)

# SOPS-encrypted k8s service-account token. The only caller that needs a token
# to talk to the cluster is the web profile; there's no CLI-profile equivalent.
_K8S_TOKEN_SOPS_PATH = "secrets/claude-web-k8s-token.yaml"
_K8S_TOKEN_SOPS_EXTRACT = '["k8s_token"]'

# Fallback CA bundle on web containers — already contains Anthropic's
# swp-ca-production.crt, so it works for kubectl → egress proxy → k8s even when
# the session hasn't yet populated `<session_dir>/auth-proxy/combined_ca.pem`.
_SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")


def _resolve_project_dir() -> Path:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        raise RuntimeError("CLAUDE_PROJECT_DIR not set — cannot locate repo root for SOPS / profile")
    return Path(project_dir)


def _load_profile(project_dir: Path) -> ProfileConfig:
    profile_path = os.environ.get("DUCKTAPE_CLAUDE_HOOKS_PROFILE")
    if not profile_path:
        raise RuntimeError(
            "DUCKTAPE_CLAUDE_HOOKS_PROFILE not set — cannot load profile config. "
            "This CLI is only meant to run inside a Claude Code session where the "
            "hook daemon has already loaded a profile."
        )
    return ProfileConfig.load(project_dir / profile_path)


def _decrypt_k8s_token(project_dir: Path) -> str:
    sops_path = project_dir / _K8S_TOKEN_SOPS_PATH
    if not sops_path.is_file():
        raise RuntimeError(f"k8s token SOPS file not found: {sops_path}")
    # Subprocess `sops` — the daemon does not depend on a Python SOPS library,
    # and keeping this identical to other try_export-style decrypts means
    # SOPS_AGE_KEY handling is consistent across all secret consumers.
    result = subprocess.run(
        ["sops", "-d", "--extract", _K8S_TOKEN_SOPS_EXTRACT, str(sops_path)], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sops -d {sops_path} failed (exit {result.returncode}): {result.stderr.decode(errors='replace').strip()}"
        )
    token = result.stdout.decode(errors="replace").strip()
    if not token:
        raise RuntimeError(f"sops decrypted empty k8s token from {sops_path}")
    return token


def _resolve_ca_path() -> Path | None:
    """Pick a CA bundle for kubectl → k8s API TLS verification.

    Preference order:
      1. `<session_dir>/auth-proxy/combined_ca.pem` if the web profile has
         already materialized it via `proxy_setup.py` (system CAs + Anthropic
         MITM CA).
      2. `/etc/ssl/certs/ca-certificates.crt` — web containers have Anthropic's
         CA installed into the system store, so this works even before the
         session's combined bundle exists.
      3. None — caller's responsibility (unusual).
    """
    session_dir = os.environ.get("DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR")
    if session_dir:
        combined = Path(session_dir) / "auth-proxy" / "combined_ca.pem"
        if combined.is_file():
            return combined
    if _SYSTEM_CA_BUNDLE.is_file():
        return _SYSTEM_CA_BUNDLE
    return None


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        print("usage: claude-hook write-kubeconfig <output-path>", file=sys.stderr)
        sys.exit(2)

    output_path = Path(argv[0])

    project_dir = _resolve_project_dir()
    profile = _load_profile(project_dir)
    if profile.k8s is None:
        raise RuntimeError(
            "Active profile has no `k8s:` block — nothing to write a kubeconfig for. "
            "The CLI profile intentionally uses ~/.kube/config; do not call "
            "`claude-hook write-kubeconfig` there."
        )

    token = _decrypt_k8s_token(project_dir)
    ca_path = _resolve_ca_path()
    proxy_url = get_proxy_url(dict(os.environ))

    kubeconfig = build_kubeconfig(
        token=token,
        server=profile.k8s.server,
        service_account=profile.k8s.service_account,
        namespace=profile.k8s.namespace,
        ca_path=ca_path,
        proxy_url=proxy_url,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic 0o600 write: create a temp file in the destination directory with
    # restrictive perms from inception (never mode 0o644 even briefly), write the
    # serialized kubeconfig, then rename over the final path.
    serialized = yaml.safe_dump(kubeconfig, default_flow_style=False, sort_keys=False)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(output_path)
    print(
        f"wrote {output_path} — server={profile.k8s.server} ca={ca_path} proxy={'set' if proxy_url else 'unset'}",
        file=sys.stderr,
    )
