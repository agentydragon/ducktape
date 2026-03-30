"""Detect the Claude Code web container platform at session start.

The web environment has migrated from gVisor (9p root filesystem) to
Firecracker microVMs (real Linux kernel, ext4 on virtio disk). This module
detects which platform we're running on so session setup can adapt:

- **Firecracker**: ext4 root, overlay works natively, no tmpfs needed for
  Bazel cache or Docker storage. JVM heap should be sized for full-monorepo
  Skyframe analysis (~8Gi).
- **gVisor** (legacy): 9p root, no overlay support, tmpfs required for
  fast I/O. Smaller JVM heap due to tmpfs eating RAM.

See devinfra/claude/web_env/docs/container_spec.md for the current
environment specification and IO benchmarks.
"""

import enum
import logging
import platform
import socket
from dataclasses import dataclass
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# Known platform indicators (see container_spec.md for details).
_GVISOR_HOSTNAME = "runsc"
_FIRECRACKER_INIT_FLAG = "--firecracker-init"


class WebPlatform(enum.Enum):
    """Detected platform variant for Claude Code web containers."""

    FIRECRACKER = "firecracker"
    GVISOR = "gvisor"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlatformInfo:
    """Detected platform characteristics for the web container."""

    hostname: str
    root_fstype: str
    init_cmdline: list[str]
    kernel_version: str
    platform: WebPlatform

    @property
    def is_firecracker(self) -> bool:
        return self.platform == WebPlatform.FIRECRACKER

    @property
    def is_gvisor(self) -> bool:
        return self.platform == WebPlatform.GVISOR

    @property
    def root_supports_overlay(self) -> bool:
        """True if the root filesystem supports overlay mounts natively."""
        return self.root_fstype in ("ext4", "xfs", "btrfs")

    @property
    def needs_tmpfs_for_io(self) -> bool:
        """True if tmpfs is needed for acceptable Bazel/Docker I/O performance.

        On gVisor, the 9p root is ~10x slower than tmpfs. IO benchmarks
        (2026-03-30, Firecracker ext4 vs tmpfs):
          Seq write: 98 MB/s (ext4) vs 345 MB/s (tmpfs)
          Seq read:  241 MB/s (ext4) vs 2.4 GB/s (tmpfs)
          4K write:  92 MB/s (ext4) vs 1.3 GB/s (tmpfs)
        On Firecracker with ext4, disk I/O is adequate and tmpfs would
        waste RAM. See container_spec.md for full benchmarks.
        """
        return not self.root_supports_overlay


def _classify_platform(hostname: str, root_fstype: str, init_cmdline: list[str]) -> WebPlatform:
    """Classify the platform based on detected indicators."""
    if any(_FIRECRACKER_INIT_FLAG in arg for arg in init_cmdline):
        return WebPlatform.FIRECRACKER
    if root_fstype == "9p" or hostname == _GVISOR_HOSTNAME:
        return WebPlatform.GVISOR
    return WebPlatform.UNKNOWN


def detect() -> PlatformInfo:
    """Probe the runtime environment and return platform info.

    Safe to call early in session start — reads only /proc and psutil.
    """
    hostname = _read_hostname()
    root_fstype = _detect_root_fstype()
    init_cmdline = _read_init_cmdline()
    kernel_version = _read_kernel_version()
    platform = _classify_platform(hostname, root_fstype, init_cmdline)

    info = PlatformInfo(
        hostname=hostname,
        root_fstype=root_fstype,
        init_cmdline=init_cmdline,
        kernel_version=kernel_version,
        platform=platform,
    )

    logger.info(
        "Platform: %s hostname=%s root_fstype=%s kernel=%s", platform.value, hostname, root_fstype, kernel_version
    )

    if platform == WebPlatform.UNKNOWN:
        logger.warning(
            "Unexpected platform state: hostname=%r root_fstype=%r init_cmdline=%r. "
            "Neither Firecracker nor gVisor indicators matched. "
            "Platform-specific optimizations (tmpfs, overlay, JVM heap) will use "
            "conservative defaults. Please notify the user about this unexpected state. "
            "See devinfra/claude/web_env/docs/container_spec.md for expected indicators.",
            hostname,
            root_fstype,
            init_cmdline[:200],
        )

    return info


def _read_hostname() -> str:
    try:
        return Path("/etc/hostname").read_text().strip()
    except OSError:
        return socket.gethostname()


def _detect_root_fstype() -> str:
    """Detect the filesystem type of the root mount."""
    for part in psutil.disk_partitions(all=True):
        if part.mountpoint == "/":
            return part.fstype
    return "unknown"


def _read_init_cmdline() -> list[str]:
    """Read PID 1's command line to detect Firecracker init."""
    try:
        return psutil.Process(1).cmdline()
    except (psutil.Error, OSError):
        return []


def _read_kernel_version() -> str:
    return platform.release()
