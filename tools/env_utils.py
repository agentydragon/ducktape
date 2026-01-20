"""Utilities for working with environment variables.

Shared utilities for getting required/optional environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_required_env(name: str) -> str:
    """Get required environment variable or raise.

    Args:
        name: Environment variable name

    Returns:
        The environment variable value

    Raises:
        KeyError: If environment variable is not set

    Example:
        >>> api_key = get_required_env("API_KEY")
    """
    return os.environ[name]


def get_required_env_path(name: str) -> Path:
    """Get required environment variable as a Path or raise.

    Args:
        name: Environment variable name

    Returns:
        Path object for the environment variable value

    Raises:
        KeyError: If environment variable is not set

    Example:
        >>> project_dir = get_required_env_path("CLAUDE_PROJECT_DIR")
    """
    return Path(os.environ[name])


def get_optional_env(name: str, default: str | None = None) -> str | None:
    """Get optional environment variable.

    Args:
        name: Environment variable name
        default: Default value if not set

    Returns:
        The environment variable value, default, or None

    Example:
        >>> debug = get_optional_env("DEBUG", "false")
    """
    return os.environ.get(name) or default


def get_optional_env_path(name: str) -> Path | None:
    """Get optional environment variable as a Path.

    Args:
        name: Environment variable name

    Returns:
        Path object if set, None otherwise

    Example:
        >>> cache_dir = get_optional_env_path("CACHE_DIR")
    """
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value)
