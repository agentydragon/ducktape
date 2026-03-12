"""Print env vars for Claude Code web session k8s token setup.

Reads the long-lived ServiceAccount token from the declarative Secret
(type kubernetes.io/service-account-token) and prints the env var to
configure in Claude Code's environment settings.

Run via: bazel run //cluster/scripts:generate_claude_kubeconfig
"""

import base64
import logging
import os
from pathlib import Path

from kubernetes import client, config

from devinfra.claude_hooks.k8s_secrets_setup import HOOKS_DOTDIR, load_config
from util.bazel.workspace import get_build_workspace_directory

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

SA_TOKEN_SECRET_NAME = "claude-code-web-token"


def generate(root: Path) -> None:
    config_path = root / HOOKS_DOTDIR / "config.yaml"
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    hook_config = load_config(config_path)
    k8s_cfg = hook_config.k8s
    server = k8s_cfg.server
    service_account = k8s_cfg.service_account
    sa_namespace = k8s_cfg.sa_namespace

    kubeconfig_path = os.environ.get("KUBECONFIG")
    if not kubeconfig_path:
        raise SystemExit("KUBECONFIG not set — run from cluster/ with direnv or set it manually")

    log.info("Loading kubeconfig from %s", kubeconfig_path)
    config.load_kube_config(kubeconfig_path)

    log.info("Reading token from Secret %s/%s", sa_namespace, SA_TOKEN_SECRET_NAME)
    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(SA_TOKEN_SECRET_NAME, sa_namespace)
    # Secret data values are base64-encoded by the K8s API
    token = base64.b64decode(secret.data["token"]).decode()

    print()
    print("# Add the following to Claude Code's environment configuration:")
    print(f"DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN={token}")
    print()
    print(f"# K8s API server: {server}")
    print(f"# Service account: {service_account} (namespace: {sa_namespace})")
    print("# Token is long-lived (declarative Secret, no expiry)")


def main() -> None:
    generate(get_build_workspace_directory())


if __name__ == "__main__":
    main()
