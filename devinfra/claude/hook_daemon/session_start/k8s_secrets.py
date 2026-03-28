"""K8s-based secrets setup for Claude Code sessions.

Reads secrets from Kubernetes Secrets in the cluster using a ServiceAccount token,
and builds a kubeconfig for kubectl access.

Config mapping (secret name, data keys, env vars) is defined in
.claude_hooks/config.yaml under the k8s_secrets section.
"""

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from kubernetes import client as k8s_client
from kubernetes.client import Configuration, CoreV1Api

from devinfra.claude.auth_proxy.vars import normalize_proxy_url
from devinfra.claude.hook_config import HookConfig, K8sSecretRef

logger = logging.getLogger(__name__)


@dataclass
class K8sSecretsResult:
    """Result of k8s secrets setup."""

    env_vars: dict[str, str] = field(default_factory=dict)
    kubeconfig_path: Path | None = None
    buildbuddy_api_key: str | None = None
    otel_bearer_token: str | None = None


def _read_secret_ref(api: CoreV1Api, ref: K8sSecretRef, namespace: str) -> str | None:
    """Read a single key from a k8s Secret, returning the decoded value or None."""
    try:
        secret = api.read_namespaced_secret(ref.secret_name, namespace)
        if secret.data and ref.data_key in secret.data:
            value = base64.b64decode(secret.data[ref.data_key]).decode()
            logger.info("Read %s/%s[%s]", namespace, ref.secret_name, ref.data_key)
            return value
        logger.warning("Key %r not found in secret %s/%s", ref.data_key, namespace, ref.secret_name)
    except k8s_client.ApiException as e:
        logger.warning("Failed to read secret %s/%s: %s", namespace, ref.secret_name, e.reason)
    return None


def _build_kubeconfig(
    token: str, server: str, service_account: str, namespace: str, ca_path: Path | None, proxy_url: str | None = None
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


def setup_k8s_secrets(
    token: str, session_dir: Path, combined_ca_path: Path | None, config: HookConfig, proxy: str | None = None
) -> K8sSecretsResult:
    """Read secrets from k8s and write kubeconfig.

    Uses the kubernetes Python client with a Bearer token to read secrets
    from the configured namespace, maps data keys to env var names, and
    writes a kubeconfig file for kubectl CLI use.

    Pass `proxy` explicitly; callers must supply fresh credentials per hook invocation.
    """
    result = K8sSecretsResult()
    if not config.k8s or not config.k8s_secrets:
        raise ValueError(f"k8s and k8s_secrets config sections are required, got {config.k8s=}, {config.k8s_secrets=}")
    k8s_cfg = config.k8s
    secrets_cfg = config.k8s_secrets

    # Configure k8s client
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

    api = CoreV1Api(k8s_client.ApiClient(client_config))

    # Read each secret and map to env vars
    for entry in secrets_cfg.secrets:
        try:
            secret = api.read_namespaced_secret(entry.name, secrets_cfg.namespace)
        except k8s_client.ApiException as e:
            logger.warning("Failed to read secret %s/%s: %s", secrets_cfg.namespace, entry.name, e.reason)
            continue

        if not secret.data:
            logger.warning("Secret %s/%s has no data", secrets_cfg.namespace, entry.name)
            continue

        for data_key, env_var in entry.data.items():
            if data_key not in secret.data:
                logger.warning("Key %r not found in secret %s/%s", data_key, secrets_cfg.namespace, entry.name)
                continue
            value = base64.b64decode(secret.data[data_key]).decode()
            result.env_vars[env_var] = value
            logger.info("Mapped %s/%s[%s] -> %s", secrets_cfg.namespace, entry.name, data_key, env_var)

    # Fetch internal-use secrets (also exported as env vars for CLI tools)
    if secrets_cfg.buildbuddy_api_key:
        result.buildbuddy_api_key = _read_secret_ref(api, secrets_cfg.buildbuddy_api_key, secrets_cfg.namespace)
        if result.buildbuddy_api_key:
            result.env_vars["BUILDBUDDY_API_KEY"] = result.buildbuddy_api_key
    if secrets_cfg.otel_bearer_token:
        result.otel_bearer_token = _read_secret_ref(api, secrets_cfg.otel_bearer_token, secrets_cfg.namespace)

    # Write kubeconfig with the full proxy URL (credentials are needed by kubectl).
    kubeconfig = _build_kubeconfig(
        token, k8s_cfg.server, k8s_cfg.service_account, k8s_cfg.namespace, combined_ca_path, proxy_url=proxy
    )
    kubeconfig_path = session_dir / "kubeconfig"
    kubeconfig_path.write_text(yaml.dump(kubeconfig, default_flow_style=False))
    kubeconfig_path.chmod(0o600)
    result.kubeconfig_path = kubeconfig_path
    result.env_vars["KUBECONFIG"] = str(kubeconfig_path)
    logger.info("Kubeconfig written to %s", kubeconfig_path)

    return result
