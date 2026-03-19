"""Consolidated apt package installation for session start.

Runs a single apt-get update + install for all system packages needed by the
session. Consolidates what was previously scattered across multiple modules
(native_deps_setup, container_runtime) into one stage.

Package groups:
- Native dev headers (always): libgirepository-2.0-dev, libcairo2-dev,
  libdbus-1-dev — needed by Bazel's hermetic pip to compile pygobject,
  pycairo, dbus-python wheels during repository fetch.
- Podman (conditional): podman, crun — only when container_runtime=podman
  and podman is not already installed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

NATIVE_DEV_PACKAGES = ["libgirepository-2.0-dev", "libcairo2-dev", "libdbus-1-dev"]
PODMAN_PACKAGES = ["podman", "crun"]


@dataclass(frozen=True)
class AptSetup:
    packages_installed: list[str]


async def install_packages(packages: list[str]) -> AptSetup:
    """Run a single apt-get update + install for the given packages."""
    if not packages:
        return AptSetup(packages_installed=[])

    logger.info("Installing system packages: %s", " ".join(packages))

    process = await asyncio.create_subprocess_exec(
        "apt-get", "update", "-qq", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    if process.returncode != 0:
        logger.warning("apt-get update failed: %s", stderr.decode())

    process = await asyncio.create_subprocess_exec(
        "apt-get", "install", "-y", "-qq", *packages, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    if process.returncode != 0:
        raise RuntimeError(f"apt-get install failed: {stderr.decode()}")

    logger.info("System packages installed successfully")
    return AptSetup(packages_installed=list(packages))
