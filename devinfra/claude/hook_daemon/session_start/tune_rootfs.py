"""Reduce ext4 reserved blocks on the root filesystem.

Anthropic's Firecracker VMs ship with 84% of ext4 blocks reserved for
nobody:nogroup (UID/GID 65534). Since the container runs as root, these
blocks are inaccessible — leaving only ~41 GiB usable on a 256 GiB disk.

Running `tune2fs -m 1 /dev/vda` reduces the reservation to 1%, freeing
~194 GiB. This is safe: no process in the container runs as nobody.
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Only act if more than this fraction of blocks are reserved.
_RESERVED_THRESHOLD = 0.10
_TARGET_RESERVED_PCT = 1
_ROOT_DEVICE = Path("/dev/vda")
_TUNE2FS = Path("/sbin/tune2fs")


def _get_reserved_ratio() -> tuple[int, int]:
    """Return (reserved_blocks, total_blocks) via os.statvfs on /."""
    st = os.statvfs("/")
    total = st.f_blocks
    # f_bfree counts all free blocks (including reserved).
    # f_bavail counts free blocks available to unprivileged users.
    # The difference is the reserved block count.
    reserved = st.f_bfree - st.f_bavail
    return reserved, total


def reduce_reserved_blocks() -> None:
    """Reduce reserved blocks on root device if excessively high."""
    if not _ROOT_DEVICE.exists():
        logger.debug("Root device %s not found, skipping", _ROOT_DEVICE)
        return
    reserved, total = _get_reserved_ratio()
    if total == 0:
        logger.warning("statvfs returned 0 total blocks for /, skipping")
        return
    ratio = reserved / total
    if ratio <= _RESERVED_THRESHOLD:
        logger.debug(
            "Reserved blocks on %s: %.1f%% (%d/%d) — below threshold, skipping",
            _ROOT_DEVICE,
            ratio * 100,
            reserved,
            total,
        )
        return
    if not _TUNE2FS.exists():
        raise FileNotFoundError(
            f"{_TUNE2FS} not found — cannot reduce {ratio * 100:.1f}% reserved blocks on {_ROOT_DEVICE}"
        )
    logger.info(
        "Reserved blocks on %s: %.1f%% (%d/%d) — reducing to %d%%",
        _ROOT_DEVICE,
        ratio * 100,
        reserved,
        total,
        _TARGET_RESERVED_PCT,
    )
    result = subprocess.run(
        [_TUNE2FS, "-m", str(_TARGET_RESERVED_PCT), _ROOT_DEVICE], capture_output=True, text=True, check=True
    )
    if result.stdout.strip():
        logger.info("tune2fs: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.info("tune2fs stderr: %s", result.stderr.strip())
