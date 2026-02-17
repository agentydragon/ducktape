"""Kubeconfig setup for Claude Code sessions.

Loads a base64-encoded kubeconfig from decrypted secrets and writes it to
a file, updating the env_vars dict to set KUBECONFIG path.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def setup_kubeconfig(cache_dir: Path, env_vars: dict[str, str]) -> Path | None:
    """Write kubeconfig from env vars to file and update KUBECONFIG env var.

    Reads KUBECONFIG_B64 from env_vars dict, decodes it, writes to file,
    and updates env_vars["KUBECONFIG"] to point to the file path.

    Args:
        cache_dir: Directory to write kubeconfig file
        env_vars: Environment variables dict (will be modified in-place to set KUBECONFIG)

    Returns:
        The kubeconfig path if successful, None if KUBECONFIG_B64 not in env_vars.
    """
    kubeconfig_b64 = env_vars.get("KUBECONFIG_B64")
    if not kubeconfig_b64:
        logger.debug("KUBECONFIG_B64 not in env_vars, skipping kubeconfig setup")
        return None

    try:
        kubeconfig_content = base64.b64decode(kubeconfig_b64).decode("utf-8")
    except Exception as e:
        logger.warning("Failed to decode KUBECONFIG_B64: %s", e)
        return None

    kubeconfig_path = cache_dir / "kubeconfig"
    kubeconfig_path.write_text(kubeconfig_content)
    kubeconfig_path.chmod(0o600)

    # Update env_vars dict to set KUBECONFIG (will be exported to shell)
    env_vars["KUBECONFIG"] = str(kubeconfig_path)

    # Remove the base64 version to avoid exposing it
    del env_vars["KUBECONFIG_B64"]

    logger.info("Kubeconfig written to %s", kubeconfig_path)
    return kubeconfig_path


def patch_kubeconfig_with_proxy_ca(kubeconfig_path: Path, proxy_ca_pem: str) -> None:
    """Append proxy CA to every cluster's certificate-authority-data in the kubeconfig.

    Needed when a TLS-inspecting proxy sits between kubectl and the API server:
    the proxy terminates TLS and presents a certificate signed by its own CA,
    so that CA must be trusted in addition to the cluster's own CA.

    Args:
        kubeconfig_path: Path to the kubeconfig file to patch (modified in place).
        proxy_ca_pem: PEM-encoded proxy CA certificate to append.
    """
    kubeconfig = yaml.safe_load(kubeconfig_path.read_text())
    clusters = kubeconfig.get("clusters") or []
    patched = 0
    for entry in clusters:
        cluster = entry.get("cluster") or {}
        existing_b64 = cluster.get("certificate-authority-data", "")
        existing_pem = base64.b64decode(existing_b64).decode("utf-8") if existing_b64 else ""
        combined_pem = existing_pem + proxy_ca_pem if existing_pem else proxy_ca_pem
        cluster["certificate-authority-data"] = base64.b64encode(combined_pem.encode()).decode()
        # Remove file-based CA reference if present — data takes precedence but kubectl
        # treats both being set as an error.
        cluster.pop("certificate-authority", None)
        entry["cluster"] = cluster
        patched += 1
    kubeconfig_path.write_text(yaml.dump(kubeconfig, default_flow_style=False))
    kubeconfig_path.chmod(0o600)
    logger.info("Patched %d cluster(s) in %s with proxy CA", patched, kubeconfig_path)
