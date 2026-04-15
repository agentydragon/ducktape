"""Kubeconfig generation for Claude Code sessions."""

import base64
import logging
from pathlib import Path

import yaml

from devinfra.claude.hook_daemon.config import K8sConfig

logger = logging.getLogger(__name__)


def build_kubeconfig(
    token: str, server: str, service_account: str, namespace: str, ca_path: Path | None, proxy_url: str | None
) -> dict:
    """Build kubeconfig dict for kubectl CLI use."""
    cluster_config: dict[str, str] = {"server": server}
    if ca_path and ca_path.exists():
        ca_pem = ca_path.read_text()
        cluster_config["certificate-authority-data"] = base64.b64encode(ca_pem.encode()).decode()
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


def write_kubeconfig(
    token: str, k8s_cfg: K8sConfig, session_dir: Path, combined_ca_path: Path | None, proxy_url: str | None
) -> Path:
    """Write kubeconfig file and return its path."""
    kc = build_kubeconfig(
        token, k8s_cfg.server, k8s_cfg.service_account, k8s_cfg.namespace, combined_ca_path, proxy_url=proxy_url
    )
    kubeconfig_path = session_dir / "kubeconfig"
    kubeconfig_path.write_text(yaml.dump(kc, default_flow_style=False))
    kubeconfig_path.chmod(0o600)
    logger.info("Kubeconfig written to %s", kubeconfig_path)
    return kubeconfig_path
