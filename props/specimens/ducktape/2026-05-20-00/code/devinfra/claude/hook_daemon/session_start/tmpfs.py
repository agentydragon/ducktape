"""Mount tmpfs volumes for fast I/O inside gVisor sessions.

gVisor's 9p root filesystem is slow, so each component that needs fast I/O
(Bazel cache, Docker/Podman storage) gets its own tmpfs mount.  We avoid
`/dev/shm` because gVisor mounts it `noexec`, which breaks Bazel's JDK.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

TMPFS_SIZE = "300G"


@dataclass
class TmpfsSetup:
    """Result of tmpfs setup."""

    bazel_cache: Path | None


def is_tmpfs_mounted(path: Path) -> bool:
    """Return True if ``path`` is a tmpfs mount point."""
    path_str = str(path)
    return any(p.mountpoint == path_str and p.fstype == "tmpfs" for p in psutil.disk_partitions(all=True))


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


def unmount_tmpfs_under(root: Path) -> None:
    """Unmount all tmpfs mounts whose mountpoint is under root.

    Uses lazy unmount (-l) so the detach succeeds even if a process (e.g. a
    Bazel server daemon) still holds the directory open.
    """
    root_str = str(root)
    mounted = [
        p.mountpoint
        for p in psutil.disk_partitions(all=True)
        if p.mountpoint.startswith(root_str) and p.fstype == "tmpfs"
    ]
    for mount_point in mounted:
        result = subprocess.run(["umount", "-l", mount_point], check=False, capture_output=True)
        if result.returncode == 0:
            logger.debug("Unmounted tmpfs at %s", mount_point)
        else:
            logger.warning("Failed to unmount %s: %s", mount_point, result.stderr.decode())


def setup_bazel_cache(bazel_cache_dir: Path) -> TmpfsSetup:
    """Set up Bazel cache at the given directory (tmpfs should already be mounted).

    The session bazelrc injects ``startup --output_user_root`` pointing here,
    so Bazel uses this directory without any global symlinks.
    """
    bazel_cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Bazel cache at %s", bazel_cache_dir)
    return TmpfsSetup(bazel_cache=bazel_cache_dir)
