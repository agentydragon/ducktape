"""Materialize a service-account kubeconfig from SOPS-encrypted secrets.

Standalone repo-specific script (not part of the generic hook daemon).
Invoked as a profile background command during SessionStart and by the
claude-sandbox-kubectl MCP server script.

Usage:
    python3 -m devinfra.claude.scripts.write_kubeconfig [OPTIONS] OUTPUT_PATH

Requires CLAUDE_PROJECT_DIR (to locate secrets/claude-web-k8s-token.yaml)
and SOPS_AGE_KEY (for sops decryption) in the environment.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

import yaml

_K8S_TOKEN_SOPS_PATH = "secrets/claude-web-k8s-token.yaml"
_K8S_TOKEN_SOPS_EXTRACT = '["k8s_token"]'
_SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")

_DEFAULT_SERVER = "https://api.allegedly.works"
_DEFAULT_SERVICE_ACCOUNT = "claude-code-web"
_DEFAULT_NAMESPACE = "claude-sandbox"


def decrypt_k8s_token(project_dir: Path) -> str:
    sops_path = project_dir / _K8S_TOKEN_SOPS_PATH
    if not sops_path.is_file():
        raise RuntimeError(f"k8s token SOPS file not found: {sops_path}")
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


def build_kubeconfig(
    token: str, server: str, service_account: str, namespace: str, ca_path: Path | None, proxy_url: str | None
) -> dict:
    cluster_config: dict[str, str] = {"server": server}
    if ca_path and ca_path.exists():
        cluster_config["certificate-authority-data"] = base64.b64encode(ca_path.read_bytes()).decode()
    if proxy_url:
        cluster_config["proxy-url"] = proxy_url

    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"cluster": cluster_config, "name": "cluster"}],
        "contexts": [
            {
                "context": {"cluster": "cluster", "namespace": namespace, "user": service_account},
                "name": service_account,
            }
        ],
        "current-context": service_account,
        "users": [{"name": service_account, "user": {"token": token}}],
    }


def write_kubeconfig_file(kubeconfig: dict, output_path: Path) -> None:
    """Atomic 0o600 write — never clobbers.

    If the file exists and its parsed YAML differs from `kubeconfig`, raises.
    A match is a no-op; a missing file is written fresh.
    """
    if output_path.exists():
        existing_raw = output_path.read_text()
        try:
            existing = yaml.safe_load(existing_raw)
        except yaml.YAMLError as e:
            raise RuntimeError(f"refusing to overwrite {output_path}: existing file is not valid YAML ({e})") from e
        if existing != kubeconfig:
            raise RuntimeError(
                f"refusing to overwrite {output_path}: existing kubeconfig differs from the one we'd write"
            )
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write a k8s service-account kubeconfig from SOPS secrets.")
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--server", default=_DEFAULT_SERVER)
    parser.add_argument("--service-account", default=_DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument("--namespace", default=_DEFAULT_NAMESPACE)
    args = parser.parse_args(argv)

    project_dir_str = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir_str:
        print("CLAUDE_PROJECT_DIR not set", file=sys.stderr)
        sys.exit(1)
    project_dir = Path(project_dir_str)

    token = decrypt_k8s_token(project_dir)
    ca_path = _SYSTEM_CA_BUNDLE if _SYSTEM_CA_BUNDLE.is_file() else None
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )

    kubeconfig = build_kubeconfig(
        token=token,
        server=args.server,
        service_account=args.service_account,
        namespace=args.namespace,
        ca_path=ca_path,
        proxy_url=proxy_url,
    )
    write_kubeconfig_file(kubeconfig, args.output_path)
    print(
        f"wrote {args.output_path} — server={args.server} ca={ca_path} proxy={'set' if proxy_url else 'unset'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
