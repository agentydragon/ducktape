"""Identity for globally-addressed semantic content."""

from __future__ import annotations

import hashlib


def content_sha(content: str) -> str:
    """Return SHA-256 of the exact normalized UTF-8 input sent to an embedder."""
    return hashlib.sha256(content.encode()).hexdigest()
