"""Materialize a bearer-token kubeconfig from a SOPS-encrypted JWT.

Why bearer token, not x509 client cert: the Claude Code web egress proxy is
an L7 TLS-terminating MITM (presents an Anthropic-signed cert for every
destination, opens a fresh upstream TLS connection). Client certificates
live in the TLS handshake and die at the proxy boundary — they cannot be
relayed upstream. Bearer tokens ride in the HTTP Authorization header and
survive the proxy's re-encryption.

Why Authentik-issued JWT, not a ServiceAccount token: SA tokens authenticate
as `system:serviceaccount:<ns>:<name>` — a principal with no group claim.
Reusing the cluster's sandbox RBAC would require adding the SA as an explicit
subject to every Role/ClusterRoleBinding it needs (see commit ff3ac18e0 for
the ~30-binding cleanup we don't want to undo). Authentik's
`kubectl-sandbox-client-credentials` provider ships a scope mapping that
hardcodes `groups: ["kubectl-sandbox-users"]` on issued JWTs; kube-apiserver's
AuthenticationConfiguration maps that claim to
`oidc-ksbx-groups:kubectl-sandbox-users`, which every sandbox RoleBinding
already subjects on. Zero RBAC edits.

The JWT is minted in-cluster by the `claude-jwt-rotation` CronJob (a
client_credentials exchange against Authentik's token endpoint) and committed
to `secrets/claude-web-k8s-jwt.yaml`, SOPS-encrypted. This script decrypts
it at SessionStart with the recipient's `SOPS_AGE_KEY` — no HTTP calls from
here, no client_secret on the sandbox.

Server is https://kubeapi.allegedly.works — an HTTPRoute on the Cilium
Gateway terminating the wildcard LE cert and re-encrypting to kube-apiserver.
The legacy api.allegedly.works TLS-passthrough route is untouched by this
migration; laptop kubectl uses an unrelated admin kubeconfig deployed by
home-manager. See cluster/docs/lessons_learned/
2026_04_24_k8s_auth_through_mitm_proxy.md for the full investigation.

Usage:
    python3 "$CLAUDE_PROJECT_DIR/devinfra/claude/scripts/write_kubeconfig.py" \\
        [OPTIONS] OUTPUT_PATH

Requires CLAUDE_PROJECT_DIR (to locate secrets/claude-web-k8s-jwt.yaml)
and SOPS_AGE_KEY (for sops decryption) in the environment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_K8S_JWT_SOPS_PATH = "secrets/claude-web-k8s-jwt.yaml"

_DEFAULT_SERVER = "https://kubeapi.allegedly.works"
_DEFAULT_USER = "claude-code-web"
_DEFAULT_NAMESPACE = "claude-sandbox"


def _sops_extract(sops_path: Path, key: str) -> str:
    result = subprocess.run(["sops", "-d", "--extract", f'["{key}"]', str(sops_path)], capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"sops -d --extract {key} {sops_path} failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    value = result.stdout.decode(errors="replace").strip()
    if not value:
        raise RuntimeError(f"sops decrypted empty {key} from {sops_path}")
    return value


def decrypt_jwt(project_dir: Path) -> str:
    """Return the JWT from the SOPS-encrypted file."""
    sops_path = project_dir / _K8S_JWT_SOPS_PATH
    if not sops_path.is_file():
        raise RuntimeError(f"k8s JWT SOPS file not found: {sops_path}")
    return _sops_extract(sops_path, "jwt")


def build_kubeconfig(token: str, server: str, user: str, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"cluster": {"server": server}, "name": "cluster"}],
        "contexts": [{"context": {"cluster": "cluster", "namespace": namespace, "user": user}, "name": user}],
        "current-context": user,
        "users": [{"name": user, "user": {"token": token}}],
    }


def write_kubeconfig_file(kubeconfig: dict, output_path: Path) -> None:
    """Atomic 0o600 write — never clobbers a non-empty foreign kubeconfig.

    - Missing file or empty file (yaml-parses to None): write fresh.
      Empty-file tolerance lets callers do `mktemp` (which creates a 0-byte
      file) and pass that path here.
    - File parses to a YAML doc identical to `kubeconfig`: no-op.
    - File parses to anything else: raise — refusing to clobber a foreign
      kubeconfig (e.g., user's existing ~/.kube/config).
    """
    if output_path.exists():
        existing_raw = output_path.read_text()
        try:
            existing = yaml.safe_load(existing_raw)
        except yaml.YAMLError as e:
            raise RuntimeError(f"refusing to overwrite {output_path}: existing file is not valid YAML ({e})") from e
        if existing is None:
            pass  # 0-byte placeholder (e.g., from mktemp) — treat as fresh write
        elif existing == kubeconfig:
            return
        else:
            raise RuntimeError(
                f"refusing to overwrite {output_path}: existing kubeconfig differs from the one we'd write"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = yaml.safe_dump(kubeconfig, default_flow_style=False, sort_keys=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
        tmp_path.chmod(0o600)
        tmp_path.replace(output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write a k8s bearer-token kubeconfig from SOPS secrets.")
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--server", default=_DEFAULT_SERVER)
    parser.add_argument("--user", default=_DEFAULT_USER)
    parser.add_argument("--namespace", default=_DEFAULT_NAMESPACE)
    args = parser.parse_args(argv)

    project_dir_str = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir_str:
        print("CLAUDE_PROJECT_DIR not set", file=sys.stderr)
        sys.exit(1)
    project_dir = Path(project_dir_str)

    token = decrypt_jwt(project_dir)

    kubeconfig = build_kubeconfig(token=token, server=args.server, user=args.user, namespace=args.namespace)
    write_kubeconfig_file(kubeconfig, args.output_path)
    print(
        f"wrote {args.output_path} — server={args.server} user={args.user} namespace={args.namespace}", file=sys.stderr
    )


if __name__ == "__main__":
    main()
