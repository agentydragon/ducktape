"""Age-encrypted secrets decryption for session hooks.

Decrypts secrets.env.age using the age key from HookSettings and appends the
raw decrypted shell script to the env file. Also reads extra_context.md
for injection into the session context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pyrage

logger = logging.getLogger(__name__)

_DEFAULT_SECRETS_DIR = Path(__file__).parent / "secrets"


@dataclass
class SecretsSetup:
    """Result of secrets decryption."""

    env_exports: str
    extra_context: str


def setup_secrets(age_key: str | None, secrets_dir: Path | None = None) -> SecretsSetup | None:
    """Decrypt secrets and prepare env exports + context.

    Returns None if age_key is not set.
    """
    if not age_key:
        return None

    resolved_dir = secrets_dir or _DEFAULT_SECRETS_DIR
    encrypted_file = resolved_dir / "secrets.env.age"
    extra_context_file = resolved_dir / "extra_context.md"

    identity = pyrage.x25519.Identity.from_str(age_key.strip())
    encrypted = encrypted_file.read_bytes()
    plaintext: str = pyrage.decrypt(encrypted, [identity]).decode()

    extra_context = ""
    if extra_context_file.exists():
        extra_context = extra_context_file.read_text().strip()

    logger.info("Decrypted secrets from %s", encrypted_file)

    return SecretsSetup(env_exports=plaintext.rstrip(), extra_context=extra_context)
