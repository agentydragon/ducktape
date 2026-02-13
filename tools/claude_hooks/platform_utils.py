"""Platform detection utilities for tool downloads."""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass
class PlatformInfo:
    """Normalized platform identifiers for binary downloads."""

    system: str  # "linux" or "darwin"
    arch: str  # "amd64" or "arm64"


def get_platform() -> PlatformInfo:
    """Detect current platform, normalized for GitHub release binary names.

    Raises RuntimeError for unsupported OS or architecture.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    if system not in ("linux", "darwin"):
        raise RuntimeError(f"Unsupported OS: {system}")

    return PlatformInfo(system=system, arch=arch)
