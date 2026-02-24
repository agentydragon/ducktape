"""Generate age-encrypted kubeconfig for Claude Code sessions.

Creates a ServiceAccount token for claude-code-web, builds a kubeconfig,
and age-encrypts it to .claude_hooks/secrets/kubeconfig.age.

Run via: bazel run //cluster/scripts:generate_claude_kubeconfig
"""

import logging
import os
from pathlib import Path

import pyrage
import pyrage.x25519
from kubernetes import client, config

from tools.claude_hooks.kubeconfig_setup import KubeconfigSecret
from util.bazel.workspace import get_build_workspace_directory

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

SERVER = "https://api.allegedly.works:16443"
SERVICE_ACCOUNT = "claude-code-web"
SA_NAMESPACE = "default"
TOKEN_EXPIRY_SECONDS = 365 * 24 * 3600  # 1 year


def generate(root: Path) -> None:
    recipients_file = root / ".claude_hooks" / "secrets" / "recipients.txt"
    output_file = root / ".claude_hooks" / "secrets" / "kubeconfig.age"

    if not recipients_file.exists():
        raise SystemExit(f"No recipients.txt found at {recipients_file}")

    kubeconfig_path = os.environ.get("KUBECONFIG")
    if not kubeconfig_path:
        raise SystemExit("KUBECONFIG not set — run from cluster/ with direnv or set it manually")

    log.info("Loading kubeconfig from %s", kubeconfig_path)
    config.load_kube_config(kubeconfig_path)

    log.info("Creating 1-year token for %s/%s", SA_NAMESPACE, SERVICE_ACCOUNT)
    v1 = client.CoreV1Api()
    # Empty audiences list lets the API server use its default audience,
    # which matches what kubectl uses for authentication tokens.
    token_request = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=TOKEN_EXPIRY_SECONDS)
    )
    resp = v1.create_namespaced_service_account_token(SERVICE_ACCOUNT, SA_NAMESPACE, token_request)
    token = resp.status.token

    # No internal CA needed — the proxy uses a publicly-trusted LE certificate.
    secret = KubeconfigSecret(server=SERVER, token=token)

    recipient_str = recipients_file.read_text().strip()
    recipient = pyrage.x25519.Recipient.from_str(recipient_str)
    encrypted = pyrage.encrypt(secret.model_dump_json().encode(), [recipient])
    output_file.write_bytes(encrypted)

    log.info("Claude kubeconfig written to %s", output_file.relative_to(root))
    log.info("Commit the updated kubeconfig.age to complete the update")


def main() -> None:
    generate(get_build_workspace_directory())


if __name__ == "__main__":
    main()
