"""Directory locations for gmail-archiver."""

from pathlib import Path

import platformdirs

APP_NAME = "gmail-archiver"


def get_cache_dir() -> Path:
    """Get cache directory for gmail-archiver."""
    cache_dir = platformdirs.user_cache_path(APP_NAME)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
