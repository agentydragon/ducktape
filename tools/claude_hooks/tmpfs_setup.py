"""Set up tmpfs-backed directories for better performance.

gVisor's 9p root filesystem is slow (~30GB).  This module mounts a dedicated
tmpfs and symlinks the Bazel cache there for faster I/O.  Podman also uses
this tmpfs for overlay storage (see ``podman_service.py``).

We do NOT use /dev/shm because gVisor mounts it ``noexec``, which prevents
Bazel's embedded JDK and external toolchain binaries from executing.  Instead
we ``mount -t tmpfs`` our own volume (inherits no ``noexec`` restriction).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TMPFS_MOUNT = Path("/mnt/bazel-tmpfs")
TMPFS_SIZE = "300G"
BAZEL_CACHE_NAME = "bazel-cache"


@dataclass
class TmpfsSetup:
    """Result of tmpfs setup."""

    bazel_cache: Path | None


def ensure_tmpfs_mounted() -> Path:
    """Mount a dedicated exec-capable tmpfs at ``TMPFS_MOUNT``.

    Returns the tmpfs root path.  Idempotent — skips if already mounted.
    """
    TMPFS_MOUNT.mkdir(parents=True, exist_ok=True)

    # Check if already mounted
    with Path("/proc/mounts").open() as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == str(TMPFS_MOUNT):
                logger.info("tmpfs already mounted at %s", TMPFS_MOUNT)
                return TMPFS_MOUNT

    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", f"size={TMPFS_SIZE}", "tmpfs", str(TMPFS_MOUNT)], check=True, capture_output=True
    )
    logger.info("Mounted tmpfs (%s) at %s", TMPFS_SIZE, TMPFS_MOUNT)
    return TMPFS_MOUNT


def setup_bazel_cache(tmpfs_root: Path) -> TmpfsSetup:
    """Set up Bazel cache on the already-mounted tmpfs.

    Symlinks ``~/.cache/bazel`` to a directory on the tmpfs.
    If ``~/.cache/bazel`` already exists and is not a symlink, moves its
    contents to tmpfs first.
    """
    bazel_cache: Path | None = None

    try:
        tmpfs_cache = tmpfs_root / BAZEL_CACHE_NAME
        local_cache = Path.home() / ".cache" / "bazel"

        # Create tmpfs directory
        tmpfs_cache.mkdir(parents=True, exist_ok=True)
        logger.info("Created Bazel tmpfs cache at %s", tmpfs_cache)

        # Handle existing local cache
        if local_cache.is_symlink():
            target = local_cache.resolve()
            if target == tmpfs_cache:
                logger.info("Bazel cache already symlinked to tmpfs")
                return TmpfsSetup(bazel_cache=tmpfs_cache)
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

        bazel_cache = tmpfs_cache
    except Exception as e:
        logger.warning("Failed to set up Bazel tmpfs cache: %s", e)

    return TmpfsSetup(bazel_cache=bazel_cache)
