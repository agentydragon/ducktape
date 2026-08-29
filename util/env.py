"""Utilities for working with environment variables.

Shared utilities for getting required/optional environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field

# Config fields that NAME an environment variable rather than carry its value: one shared
# uppercase-POSIX vocabulary, so every site validates identically.
EnvironmentVariableName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


def get_required_env(name: str) -> str:
    """Get required environment variable or raise."""
    return os.environ[name]


def get_required_env_path(name: str) -> Path:
    """Get required environment variable as a Path or raise."""
    return Path(os.environ[name])


def get_required_existing_path(name: str) -> Path:
    """Get required environment variable as a Path, verifying the path exists."""
    path = Path(os.environ[name])
    if not path.exists():
        raise FileNotFoundError(f"{name}={path} does not exist")
    return path


def get_optional_env(name: str, default: str | None = None) -> str | None:
    """Get optional environment variable."""
    return os.environ.get(name) or default


def get_optional_env_path(name: str) -> Path | None:
    """Get optional environment variable as a Path."""
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value)
