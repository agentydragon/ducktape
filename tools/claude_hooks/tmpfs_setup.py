"""Set up tmpfs-backed directories for better performance.

gVisor's 9p root filesystem is slow.  This module mounts dedicated tmpfs
volumes for components that need fast I/O (Bazel cache, Docker storage,
Podman overlay).  Each component gets its own tmpfs mount under the session
directory.

We do NOT use /dev/shm because gVisor mounts it ``noexec``, which prevents
Bazel's embedded JDK and external toolchain binaries from executing.  Instead
we ``mount -t tmpfs`` our own volumes (inherit no ``noexec`` restriction).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TMPFS_SIZE = "300G"


@dataclass
class TmpfsSetup:
    """Result of tmpfs setup."""

    bazel_cache: Path | None


def is_tmpfs_mounted(path: Path) -> bool:
    """Return True if ``path`` is a tmpfs mount point."""
    path_str = str(path)
    with Path("/proc/mounts").open() as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[1] == path_str and parts[2] == "tmpfs":
                return True
    return False


def ensure_tmpfs_mounted(path: Path) -> None:
    """Mount a dedicated exec-capable tmpfs at ``path``.

    Idempotent — skips if already mounted.  Creates the directory if needed.
    """
    path.mkdir(parents=True, exist_ok=True)

    if is_tmpfs_mounted(path):
        logger.info("tmpfs already mounted at %s", path)
        return

    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", f"size={TMPFS_SIZE}", "tmpfs", str(path)], check=True, capture_output=True
    )
    logger.info("Mounted tmpfs (%s) at %s", TMPFS_SIZE, path)


def setup_bazel_cache(bazel_cache_dir: Path) -> TmpfsSetup:
    """Set up Bazel cache at the given directory (tmpfs should already be mounted).

    The session bazelrc injects ``startup --output_user_root`` pointing here,
    so Bazel uses this directory without any global symlinks.
    """
    bazel_cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Bazel cache at %s", bazel_cache_dir)
    return TmpfsSetup(bazel_cache=bazel_cache_dir)
