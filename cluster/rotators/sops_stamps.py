"""Plaintext rotation stamps in SOPS-encrypted YAML."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def unencrypted_stamps(sops_file: Path) -> dict[str, Any]:
    """Read plaintext `*_unencrypted` stamps without an age key; absent/empty files yield `{}`."""
    if not sops_file.exists():
        return {}
    return yaml.safe_load(sops_file.read_text()) or {}


def remaining_hours(stamps: Mapping[str, Any]) -> float | None:
    """Hours until the stamped expiry, or None if unstamped."""
    expires = stamps.get("expires_unencrypted")
    if expires is None:
        return None
    return (datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds() / 3600
