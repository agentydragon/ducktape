"""Kubeconfig setup for Claude Code sessions.

Loads a base64-encoded kubeconfig from decrypted secrets and writes it to
a file, updating the env_vars dict to set KUBECONFIG path.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

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
