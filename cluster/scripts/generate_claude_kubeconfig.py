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

from devinfra.claude.hook_config import HookConfig
from util.bazel.workspace import get_build_workspace_directory

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

SA_TOKEN_SECRET_NAME = "claude-code-web-token"


def generate(root: Path) -> None:
    hook_config = HookConfig.load_from_repo(root)
    if not hook_config:
        raise SystemExit(f"Config file not found under {root}")
    if not hook_config.k8s:
        raise SystemExit("No k8s config found in hook config")

    kubeconfig_path = os.environ.get("KUBECONFIG")
    if not kubeconfig_path:
        raise SystemExit("KUBECONFIG not set — run from cluster/ with direnv or set it manually")

    log.info("Loading kubeconfig from %s", kubeconfig_path)
    config.load_kube_config(kubeconfig_path)

    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(SA_TOKEN_SECRET_NAME, hook_config.k8s.service_account_namespace)
    # Secret data values are base64-encoded by the K8s API
    token = base64.b64decode(secret.data["token"]).decode()

    print(f"DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN={token}")


def main() -> None:
    generate(get_build_workspace_directory())


if __name__ == "__main__":
    main()
