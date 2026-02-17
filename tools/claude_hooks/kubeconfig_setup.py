"""Kubeconfig setup for Claude Code sessions.

Builds a kubeconfig file from typed secret fields and writes it to a file,
updating the env_vars dict to set the KUBECONFIG path.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class KubeconfigSecret(BaseModel):
    """Typed kubeconfig secret stored in kubeconfig.age.

    JSON format (produced by model_dump()):
        {"type": "kubeconfig", "server": "...", "ca_b64": "...", "token": "..."}
    """

    type: Literal["kubeconfig"] = "kubeconfig"
    server: str
    ca_b64: str  # base64-encoded cluster CA certificate PEM
    token: str


def setup_kubeconfig(
    session_dir: Path, secret: KubeconfigSecret, env_vars: dict[str, str], proxy_ca_pem: str | None = None
) -> Path:
    """Build and write a kubeconfig from typed secret fields.

    If proxy_ca_pem is provided (TLS-inspecting proxy detected), it is appended
    to the cluster CA so kubectl trusts both the proxy and the cluster's own CA.

    Sets env_vars["KUBECONFIG"] so the path is exported to the shell session.
    """
    cluster_ca_pem = base64.b64decode(secret.ca_b64).decode()
    combined_ca_pem = cluster_ca_pem + proxy_ca_pem if proxy_ca_pem else cluster_ca_pem
    combined_ca_b64 = base64.b64encode(combined_ca_pem.encode()).decode()

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {"cluster": {"certificate-authority-data": combined_ca_b64, "server": secret.server}, "name": "cluster"}
        ],
        "contexts": [
            {
                "context": {"cluster": "cluster", "namespace": "claude-sandbox", "user": "claude-code-web"},
                "name": "claude-code-web",
            }
        ],
        "current-context": "claude-code-web",
        "users": [{"name": "claude-code-web", "user": {"token": secret.token}}],
    }

    kubeconfig_path = session_dir / "kubeconfig"
    kubeconfig_path.write_text(yaml.dump(kubeconfig, default_flow_style=False))
    kubeconfig_path.chmod(0o600)

    env_vars["KUBECONFIG"] = str(kubeconfig_path)
    logger.info("Kubeconfig written to %s (proxy_ca=%s)", kubeconfig_path, proxy_ca_pem is not None)
    return kubeconfig_path
