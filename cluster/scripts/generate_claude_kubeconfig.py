"""Print env vars for Claude Code web session k8s token setup.

Creates a ServiceAccount token for claude-code-web via TokenRequest API
(1-year expiry) and prints the env var to configure in Claude Code's
environment settings.

Run via: bazel run //cluster/scripts:generate_claude_kubeconfig
"""

import logging
import os
from pathlib import Path

import yaml
from kubernetes import client, config

from util.bazel.workspace import get_build_workspace_directory

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN_EXPIRY_SECONDS = 365 * 24 * 3600  # 1 year


def generate(root: Path) -> None:
    config_path = root / ".claude_hooks" / "config.yaml"
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text())
    k8s_cfg = raw["k8s"]
    server = k8s_cfg["server"]
    service_account = k8s_cfg["service_account"]
    sa_namespace = k8s_cfg.get("sa_namespace", "default")

    kubeconfig_path = os.environ.get("KUBECONFIG")
    if not kubeconfig_path:
        raise SystemExit("KUBECONFIG not set — run from cluster/ with direnv or set it manually")

    log.info("Loading kubeconfig from %s", kubeconfig_path)
    config.load_kube_config(kubeconfig_path)

    log.info("Creating 1-year token for %s/%s", sa_namespace, service_account)
    v1 = client.CoreV1Api()
    # Empty audiences list lets the API server use its default audience,
    # which matches what kubectl uses for authentication tokens.
    token_request = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=TOKEN_EXPIRY_SECONDS)
    )
    resp = v1.create_namespaced_service_account_token(service_account, sa_namespace, token_request)
    token = resp.status.token

    print()
    print("# Add the following to Claude Code's environment configuration:")
    print(f"DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN={token}")
    print()
    print(f"# K8s API server: {server}")
    print(f"# Service account: {service_account} (namespace: {sa_namespace})")
    print(f"# Token expires in ~1 year ({TOKEN_EXPIRY_SECONDS}s)")


def main() -> None:
    generate(get_build_workspace_directory())


if __name__ == "__main__":
    main()
