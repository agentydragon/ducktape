"""Age-encrypted secrets decryption for session hooks.

Decrypts *.age files in the secrets directory using the age key from
HookSettings. Each file decrypts to either:

- A flat JSON dict[str, str] mapping env var names to values (legacy format):
      {"OLLAMA_API_KEY": "...", "OLLAMA_BASE_URL": "..."}

- A typed secret with a "type" discriminator (new format):
      {"type": "kubeconfig", "server": "...", "ca_b64": "...", "token": "..."}

Flat secrets are merged into env_vars (exported to shell). Typed secrets are
parsed into dedicated fields on SecretsSetup and never exported to the shell.

All component flat-secret dicts are merged with a disjoint-key check —
overlapping keys across files raise an error. Files that can't be decrypted
because the provided key doesn't match ("No matching keys found") are silently
skipped, enabling fine-grained access control by encrypting different
components to different recipients.

Secrets are loaded from the repo checkout (e.g. .claude_hooks/secrets/),
NOT from the installed wheel — the caller must provide secrets_dir explicitly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pyrage

from tools.claude_hooks.kubeconfig_setup import KubeconfigSecret

logger = logging.getLogger(__name__)


@dataclass
class SecretsSetup:
    """Result of secrets decryption."""

    env_vars: dict[str, str] = field(default_factory=dict)
    kubeconfig: KubeconfigSecret | None = None
    skipped_files: list[str] = field(default_factory=list)


def _handle_typed_secret(payload: dict, age_file_name: str, result: SecretsSetup) -> None:
    """Parse a typed secret payload and populate the appropriate field on result."""
    secret_type = payload["type"]
    if secret_type == "kubeconfig":
        if result.kubeconfig is not None:
            raise ValueError(f"Duplicate kubeconfig secret (from {age_file_name})")
        result.kubeconfig = KubeconfigSecret.model_validate(payload)
        logger.info("Decrypted %s (kubeconfig secret)", age_file_name)
    else:
        raise ValueError(f"Unknown secret type {secret_type!r} in {age_file_name}")


def setup_secrets(age_key: str | None, secrets_dir: Path) -> SecretsSetup | None:
    """Decrypt component secret files and merge into a single env var dict.

    Returns None if age_key is not set or secrets_dir doesn't exist.
    """
    if not age_key:
        return None

    if not secrets_dir.is_dir():
        logger.info("Secrets directory %s does not exist, skipping", secrets_dir)
        return None

    identity = pyrage.x25519.Identity.from_str(age_key.strip())

    result = SecretsSetup()
    decrypted_count = 0
    age_files = sorted(secrets_dir.glob("*.age"))

    for age_file in age_files:
        try:
            plaintext = pyrage.decrypt(age_file.read_bytes(), [identity]).decode().strip()
        except pyrage.DecryptError as e:
            if "No matching keys found" in str(e):
                logger.debug("Skipping %s (wrong key)", age_file.name)
                result.skipped_files.append(age_file.name)
                continue
            raise

        payload: dict = json.loads(plaintext)

        if "type" in payload:
            _handle_typed_secret(payload, age_file.name, result)
        else:
            component_vars: dict[str, str] = payload
            overlap = result.env_vars.keys() & component_vars.keys()
            if overlap:
                raise ValueError(f"Duplicate env var keys across age files: {overlap} (from {age_file.name})")
            result.env_vars.update(component_vars)
            logger.info("Decrypted %s (%d vars)", age_file.name, len(component_vars))

        decrypted_count += 1

    if decrypted_count == 0 and age_files:
        logger.warning(
            "Age key provided but 0/%d files decrypted (all skipped due to key mismatch). Skipped: %s",
            len(age_files),
            ", ".join(result.skipped_files),
        )
    elif result.skipped_files:
        logger.info(
            "Decrypted %d/%d component files (%d skipped: %s), %d env vars total",
            decrypted_count,
            len(age_files),
            len(result.skipped_files),
            ", ".join(result.skipped_files),
            len(result.env_vars),
        )
    else:
        logger.info(
            "Decrypted %d/%d component files, %d env vars total", decrypted_count, len(age_files), len(result.env_vars)
        )

    return result
