"""Age-encrypted secrets decryption for session hooks.

Decrypts *.age files in the secrets directory using the age key from
HookSettings. Each file decrypts to a JSON dict[str, str] mapping env var
names to values, allowing related secrets to be grouped by component
(e.g., ollama.age contains both OLLAMA_BASE_URL and OLLAMA_API_KEY).

All component dicts are merged with a disjoint-key check — overlapping
keys across files raise an error. Files that can't be decrypted because
the provided key doesn't match ("No matching keys found") are silently
skipped, enabling fine-grained access control by encrypting different
components to different recipients.

Secrets are loaded from the repo checkout (e.g. .claude_hooks/secrets/),
NOT from the installed wheel — the caller must provide secrets_dir explicitly.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pyrage

logger = logging.getLogger(__name__)


@dataclass
class SecretsSetup:
    """Result of secrets decryption."""

    env_vars: dict[str, str] = field(default_factory=dict)

    @property
    def env_exports(self) -> str:
        """Generate shell export statements from decrypted secrets."""
        return "\n".join(f"export {k}={shlex.quote(v)}" for k, v in sorted(self.env_vars.items()))


def setup_secrets(age_key: str | None, secrets_dir: Path) -> SecretsSetup | None:
    """Decrypt component secret files and merge into a single env var dict.

    Returns None if age_key is not set or secrets_dir doesn't exist.
    """
    if not age_key:
        return None

    if not secrets_dir.is_dir():
        logger.info("Secrets directory %s does not exist, skipping", secrets_dir)
        return None

    resolved_dir = secrets_dir

    identity = pyrage.x25519.Identity.from_str(age_key.strip())

    env_vars: dict[str, str] = {}
    decrypted_count = 0
    age_files = sorted(resolved_dir.glob("*.age"))

    for age_file in age_files:
        try:
            plaintext = pyrage.decrypt(age_file.read_bytes(), [identity]).decode().strip()
        except pyrage.DecryptError as e:
            if "No matching keys found" in str(e):
                logger.debug("Skipping %s (wrong key)", age_file.name)
                continue
            raise

        component_vars: dict[str, str] = json.loads(plaintext)
        overlap = env_vars.keys() & component_vars.keys()
        if overlap:
            raise ValueError(f"Duplicate env var keys across age files: {overlap} (from {age_file.name})")
        env_vars.update(component_vars)
        decrypted_count += 1
        logger.info("Decrypted %s (%d vars)", age_file.name, len(component_vars))

    logger.info("Decrypted %d/%d component files, %d env vars total", decrypted_count, len(age_files), len(env_vars))

    return SecretsSetup(env_vars=env_vars)
