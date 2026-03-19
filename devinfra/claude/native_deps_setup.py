"""Install native system packages required by Python pip wheels.

Some pip packages (pygobject, pycairo, dbus-python) need native dev headers
to compile wheels during Bazel's repository fetch phase. Without these, any
bazel query or build that transitively loads those packages fails.

Packages installed:
- libgirepository-2.0-dev — for pygobject (GObject introspection)
- libcairo2-dev — for pycairo (Cairo graphics)
- libdbus-1-dev — for dbus-python (D-Bus IPC)

Total: 3 packages, ~2.2 MB installed, ~7s on Claude Code web.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PACKAGES = ["libgirepository-2.0-dev", "libcairo2-dev", "libdbus-1-dev"]


@dataclass(frozen=True)
class NativeDepsSetup:
    packages_installed: list[str]


async def install_native_deps() -> NativeDepsSetup:
    """Install native dev packages via apt-get."""
    logger.info("Installing native dev packages: %s", " ".join(_PACKAGES))

    process = await asyncio.create_subprocess_exec(
        "apt-get", "update", "-qq", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    if process.returncode != 0:
        logger.warning("apt-get update failed: %s", stderr.decode())

    process = await asyncio.create_subprocess_exec(
        "apt-get", "install", "-y", "-qq", *_PACKAGES, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    if process.returncode != 0:
        raise RuntimeError(f"apt-get install failed: {stderr.decode()}")

    logger.info("Native dev packages installed successfully")
    return NativeDepsSetup(packages_installed=list(_PACKAGES))
