"""Set up tmpfs-backed directories for better performance.

gVisor's 9p root filesystem is slow (~30GB). /dev/shm is a 315GB tmpfs.
This module symlinks cache directories to tmpfs for faster I/O.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TMPFS_ROOT = Path("/dev/shm")
BAZEL_CACHE_NAME = "bazel-cache"


@dataclass
class TmpfsSetup:
    """Result of tmpfs setup."""

    bazel_cache: Path | None


def setup_bazel_tmpfs() -> Path:
    """Set up Bazel cache on tmpfs for faster builds.

    Creates /dev/shm/bazel-cache and symlinks ~/.cache/bazel to it.
    If ~/.cache/bazel already exists and is not a symlink, moves its
    contents to tmpfs first.

    Returns the tmpfs cache path.
    """
    tmpfs_cache = TMPFS_ROOT / BAZEL_CACHE_NAME
    local_cache = Path.home() / ".cache" / "bazel"

    # Create tmpfs directory
    tmpfs_cache.mkdir(parents=True, exist_ok=True)
    logger.info("Created Bazel tmpfs cache at %s", tmpfs_cache)

    # Handle existing local cache
    if local_cache.is_symlink():
        target = local_cache.resolve()
        if target == tmpfs_cache:
            logger.info("Bazel cache already symlinked to tmpfs")
            return tmpfs_cache
        # Remove old symlink
        local_cache.unlink()
        logger.info("Removed old symlink %s -> %s", local_cache, target)
    elif local_cache.is_dir():
        # Move existing cache contents to tmpfs
        for item in local_cache.iterdir():
            dest = tmpfs_cache / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
                logger.info("Moved %s to tmpfs", item.name)
        # Remove now-empty directory
        local_cache.rmdir()
        logger.info("Moved existing Bazel cache to tmpfs")

    # Ensure parent directory exists
    local_cache.parent.mkdir(parents=True, exist_ok=True)

    # Create symlink
    local_cache.symlink_to(tmpfs_cache)
    logger.info("Symlinked %s -> %s", local_cache, tmpfs_cache)

    return tmpfs_cache


def setup_tmpfs() -> TmpfsSetup:
    """Set up all tmpfs-backed directories.

    Currently sets up:
    - Bazel cache (~/.cache/bazel -> /dev/shm/bazel-cache)
    """
    bazel_cache: Path | None = None

    try:
        bazel_cache = setup_bazel_tmpfs()
    except Exception as e:
        logger.warning("Failed to set up Bazel tmpfs cache: %s", e)

    return TmpfsSetup(bazel_cache=bazel_cache)
