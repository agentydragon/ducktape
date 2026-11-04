"""Utilities for reading required environment variables."""

from __future__ import annotations

import os


def get_required_env(name: str) -> str:
    """Get required environment variable or raise."""
    return os.environ[name]
