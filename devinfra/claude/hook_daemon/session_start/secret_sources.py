"""Secrets resolution and K8s client setup for Claude Code sessions.

Resolves secrets from tagged-union SecretSource configs (SOPS or K8s),
and builds a kubeconfig for kubectl access when K8s sources are used.
"""

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from kubernetes import client as k8s_client
from kubernetes.client import Configuration, CoreV1Api

from devinfra.claude.auth_proxy.vars import normalize_proxy_url
from devinfra.claude.hook_config import K8sConfig, SecretSource, SopsSecretSource
from devinfra.claude.sops_decrypt import decrypt_sops_yaml

logger = logging.getLogger(__name__)


@dataclass
class SecretsResult:
    """Resolved secrets with explicit named fields."""

    k8s_token: str | None = None
    buildbuddy_api_key: str | None = None
    github_token: str | None = None
    otel_bearer_token: str | None = None
    kubeconfig_path: Path | None = None


def setup_k8s_client(token: str, k8s_cfg: K8sConfig, combined_ca_path: Path | None, proxy: str | None) -> CoreV1Api:
    """Configure and return a Kubernetes CoreV1Api client."""
    client_config = Configuration()
    client_config.host = k8s_cfg.server
    client_config.api_key = {"authorization": f"Bearer {token}"}
    if combined_ca_path and combined_ca_path.exists():
        client_config.ssl_ca_cert = str(combined_ca_path)
    else:
        client_config.verify_ssl = True
    if proxy:
        clean_proxy, proxy_headers = normalize_proxy_url(proxy)
        client_config.proxy = clean_proxy
        if proxy_headers:
            client_config.proxy_headers = proxy_headers
    return CoreV1Api(k8s_client.ApiClient(client_config))


def read_k8s_secret(api: CoreV1Api, namespace: str, secret_name: str, key: str) -> str | None:
    """Read a single key from a k8s Secret, returning the decoded value or None."""
    try:
        secret = api.read_namespaced_secret(secret_name, namespace)
        if secret.data and key in secret.data:
            value = base64.b64decode(secret.data[key]).decode()
            logger.info("Read %s/%s[%s]", namespace, secret_name, key)
            return value
        logger.warning("Key %r not found in secret %s/%s", key, namespace, secret_name)
    except k8s_client.ApiException as e:
        logger.warning("Failed to read secret %s/%s: %s", namespace, secret_name, e.reason)
    except Exception as e:
        # Network-level errors (e.g. proxy tunnel 403, connection refused) are not
        # wrapped in ApiException by the k8s client — catch them here so a stale
        # or unreachable k8s API doesn't crash the session start hook.
        logger.warning("Failed to read secret %s/%s (network error): %s", namespace, secret_name, e)
    return None


def resolve_secret(
    source: SecretSource,
    *,
    project_dir: Path,
    age_identities: list | None,
    k8s_api: CoreV1Api | None,
    k8s_namespace: str | None,
) -> str | None:
    """Resolve a single secret from its source config."""
    if isinstance(source, SopsSecretSource):
        if not age_identities:
            logger.warning("SOPS secret configured but no age_key available")
            return None
        decrypted = decrypt_sops_yaml(project_dir / source.sops_file, age_identities)
        return decrypted.get(source.key)
    if not k8s_api or not k8s_namespace:
        logger.warning("K8s secret configured but no k8s client available")
        return None
    return read_k8s_secret(k8s_api, k8s_namespace, source.secret_name, source.key)


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
    kubeconfig = build_kubeconfig(
        token, k8s_cfg.server, k8s_cfg.service_account, k8s_cfg.namespace, combined_ca_path, proxy_url=proxy_url
    )
    kubeconfig_path = session_dir / "kubeconfig"
    kubeconfig_path.write_text(yaml.dump(kubeconfig, default_flow_style=False))
    kubeconfig_path.chmod(0o600)
    logger.info("Kubeconfig written to %s", kubeconfig_path)
    return kubeconfig_path
