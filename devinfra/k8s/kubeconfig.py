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

The JWT is minted in-cluster by the `authentik-jwt-rotation` CronJob (a
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
    python3 "$CLAUDE_PROJECT_DIR/devinfra/k8s/kubeconfig.py" --write OUTPUT_PATH

Requires CLAUDE_PROJECT_DIR (to locate the JWT SOPS file) and SOPS_AGE_KEY
(for sops decryption) in the environment.

Defaults to the Claude Code web identity. Other agents (e.g. haku) override
the SOPS path, kubeconfig user, and namespace via the K8S_JWT_SOPS_PATH /
K8S_USER / K8S_NAMESPACE env vars.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

DEFAULT_SERVER = "https://kubeapi.allegedly.works"

# Claude Code web is the default identity; other agents (e.g. haku) override
# these via the K8S_JWT_SOPS_PATH / K8S_USER / K8S_NAMESPACE env vars so a
# single script materializes any agent's bearer-token kubeconfig.
_DEFAULT_K8S_JWT_SOPS_PATH = "secrets/claude-web-k8s-jwt.yaml"
DEFAULT_USER = "claude-code-web"
DEFAULT_NAMESPACE = "claude-sandbox"


def _jwt_sops_path() -> str:
    return os.environ.get("K8S_JWT_SOPS_PATH", _DEFAULT_K8S_JWT_SOPS_PATH)


def _user() -> str:
    return os.environ.get("K8S_USER", DEFAULT_USER)


def _namespace() -> str:
    return os.environ.get("K8S_NAMESPACE", DEFAULT_NAMESPACE)


def _sops_extract(sops_path: Path, key: str, *, sops_age_key: str | None) -> str:
    env = {**os.environ}
    if sops_age_key is not None:
        env["SOPS_AGE_KEY"] = sops_age_key
    result = subprocess.run(
        ["sops", "-d", "--extract", json.dumps([key]), sops_path], capture_output=True, check=True, env=env
    )
    value = result.stdout.decode(errors="replace").strip()
    if not value:
        raise RuntimeError(f"sops decrypted empty {key} from {sops_path}")
    return value


def decrypt_jwt(project_dir: Path, *, sops_age_key: str | None = None) -> str:
    """Return the JWT from the SOPS-encrypted file."""
    sops_path = project_dir / _jwt_sops_path()
    if not sops_path.is_file():
        raise RuntimeError(f"k8s JWT SOPS file not found: {sops_path}")
    return _sops_extract(sops_path, "jwt", sops_age_key=sops_age_key)


def build_kubeconfig(token: str) -> dict:
    user = _user()
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"cluster": {"server": DEFAULT_SERVER}, "name": "cluster"}],
        "contexts": [{"context": {"cluster": "cluster", "namespace": _namespace(), "user": user}, "name": user}],
        "current-context": user,
        "users": [{"name": user, "user": {"token": token}}],
    }


def _probe_token(server: str, token: str, *, timeout: float = 5.0) -> str | None:
    """Check whether a bearer token is accepted by the kube API server.

    Returns 'valid' (2xx or any non-401 HTTP response), 'invalid' (401
    Unauthorized), or None when the server is unreachable (network/TLS error).
    A 403 counts as 'valid': the token authenticated, just no permission.
    """
    req = urllib.request.Request(
        f"{server}/api",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as _:
            return "valid"
    except urllib.error.HTTPError as e:
        return "invalid" if e.code == 401 else "valid"
    except Exception:
        return None


def _check_overwrite(existing: dict, new: dict, path: Path) -> None:
    """Raise RuntimeError if overwriting `path` (containing `existing`) with `new` is unsafe.

    Allows overwriting when the existing file is our own single-cluster, single-user
    kubeconfig pointing at the same server with the same principal — i.e., a token
    refresh is the only difference. Refuses in all other cases:
    - Merged/personal kubeconfigs (multiple clusters, contexts, or users).
    - Different server or user identity.
    - New token already rejected (401) by the server.
    The existing token's probe result is informational: we allow overwriting whether
    it is still live (fresher JWT arriving) or already expired (rotation catch-up).
    """
    for field in ("clusters", "contexts", "users"):
        n = len(existing.get(field) or [])
        if n != 1:
            raise RuntimeError(
                f"refusing to overwrite {path}: existing kubeconfig has {n} {field} "
                f"(expected 1) — looks like a merged or personal config"
            )

    existing_server = existing["clusters"][0]["cluster"]["server"]
    new_server = new["clusters"][0]["cluster"]["server"]
    if existing_server != new_server:
        raise RuntimeError(
            f"refusing to overwrite {path}: server {existing_server!r} != {new_server!r}"
        )

    existing_user = existing["users"][0]["name"]
    new_user = new["users"][0]["name"]
    if existing_user != new_user:
        raise RuntimeError(
            f"refusing to overwrite {path}: user {existing_user!r} != {new_user!r}"
        )

    # Same server + user: token refresh. Probe the new token to catch a broken JWT
    # before writing it; probe the existing token for logging only.
    new_token = new["users"][0]["user"]["token"]
    if _probe_token(new_server, new_token) == "invalid":
        raise RuntimeError(
            f"refusing to write {path}: new token is rejected (401) by {new_server}"
        )


def write_kubeconfig_file(kubeconfig: dict, output_path: Path) -> None:
    """Atomic 0o600 write — never clobbers a non-empty foreign kubeconfig.

    - Missing file or empty file (yaml-parses to None): write fresh.
      Empty-file tolerance lets callers do `mktemp` (which creates a 0-byte
      file) and pass that path here.
    - File parses to a YAML doc identical to `kubeconfig`: no-op.
    - File parses to a single-cluster/user kubeconfig with the same server and
      user identity: probe the new token and allow overwrite if it's valid
      (token refresh path — see `_check_overwrite`).
    - Anything else (merged config, different server/user, invalid new token): raise.
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
            _check_overwrite(existing, kubeconfig, output_path)

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
    parser.add_argument("--write", type=Path, required=True, metavar="PATH")
    args = parser.parse_args(argv)

    project_dir_str = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir_str:
        print("CLAUDE_PROJECT_DIR not set", file=sys.stderr)
        sys.exit(1)
    project_dir = Path(project_dir_str)

    token = decrypt_jwt(project_dir)
    kubeconfig = build_kubeconfig(token)
    write_kubeconfig_file(kubeconfig, args.write)
    print(f"wrote {args.write}", file=sys.stderr)


if __name__ == "__main__":
    main()
