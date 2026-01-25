"""Install Bazelisk for Bazel version management.

Bazelisk automatically downloads and runs the correct Bazel version
based on .bazelversion or USE_BAZEL_VERSION.

TODO: Eventually unify tool installation via direnv/devenv instead of
      manual downloads in session hooks.
"""

from __future__ import annotations

import logging
import platform
import shutil
import ssl
import stat
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tools.claude_hooks.settings import HookSettings

logger = logging.getLogger(__name__)

BAZELISK_VERSION = "1.25.0"


@dataclass
class BazeliskSetup:
    """Result of bazelisk installation."""

    bazelisk_path: Path
    wrapper_path: Path
    settings: HookSettings
    bazelisk_skipped: bool = False

    @property
    def status(self) -> str:
        """Get status string for logging."""
        if self.bazelisk_skipped:
            return "skipped (wrapper installed)"

        version = get_bazelisk_version(self.settings)
        if not version:
            return "not installed"

        bazel_on_path = shutil.which("bazel")
        if bazel_on_path and Path(bazel_on_path).resolve() == self.wrapper_path.resolve():
            return f"{version} ({self.wrapper_path})"
        if self.wrapper_path.exists():
            return f"{version} (wrapper exists but not on PATH)"
        return f"{version} (no wrapper)"


def get_bazelisk_version(settings: HookSettings) -> str | None:
    """Get bazelisk version string, or None if not installed/working."""
    bazelisk_path = settings.get_bazelisk_path()
    if not bazelisk_path.exists():
        return None
    result = subprocess.run([bazelisk_path, "version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    # Return first line (e.g. "Bazelisk version: v1.25.0")
    return result.stdout.split("\n")[0].strip()


def get_bazelisk_url() -> str:
    """Get the appropriate Bazelisk download URL for this platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    # Normalize architecture names
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    if system == "linux":
        binary = f"bazelisk-linux-{arch}"
    elif system == "darwin":
        binary = f"bazelisk-darwin-{arch}"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")

    return f"https://github.com/bazelbuild/bazelisk/releases/download/v{BAZELISK_VERSION}/{binary}"


def install_bazelisk(settings: HookSettings) -> Path:
    """Download bazelisk to private location, returning the binary path.

    Installs to ~/.cache/bazel-proxy/bazelisk (private, not on PATH).
    The wrapper script in ~/.cache/bazel-proxy/bin/bazel will call this.
    Skips download if already installed.
    """
    bazel_proxy_dir = settings.get_bazel_proxy_dir()
    bazelisk_path = settings.get_bazelisk_path()

    bazel_proxy_dir.mkdir(parents=True, exist_ok=True)

    # Check if already installed
    if get_bazelisk_version(settings):
        logger.info("Bazelisk already installed: %s", bazelisk_path)
        return bazelisk_path

    url = get_bazelisk_url()
    logger.info("Downloading Bazelisk from %s", url)

    # Create SSL context with combined CA bundle (includes proxy's TLS inspection CA)
    # Use only our combined bundle to avoid issues with missing system CAs in sandboxes
    combined_ca = settings.get_bazel_combined_ca()
    ssl_context: ssl.SSLContext | None = None
    if combined_ca.exists():
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.load_verify_locations(combined_ca)
        logger.info("Using combined CA bundle for bazelisk download: %s", combined_ca)
    else:
        logger.warning("Combined CA bundle not found at %s, using default SSL context", combined_ca)

    # Download with proxy support (urllib respects https_proxy env var)
    with urllib.request.urlopen(url, timeout=60, context=ssl_context) as response:
        bazelisk_path.write_bytes(response.read())

    # Make executable
    bazelisk_path.chmod(bazelisk_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed Bazelisk to %s", bazelisk_path)

    return bazelisk_path


def install_wrapper(settings: HookSettings) -> Path:
    """Install wrapper script that sets proxy env vars before calling bazelisk.

    The wrapper is in ~/.cache/bazel-proxy/bin/bazel and calls the real
    bazelisk at ~/.cache/bazel-proxy/bazelisk.
    Also creates a bazelisk symlink for pre-commit hooks.
    Includes health checks for supervisor and proxy service.

    The wrapper reads configuration from environment variables (set via get_env_script).
    """
    wrapper_dir = settings.get_wrapper_dir()
    wrapper_path = settings.get_wrapper_path()

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    # Create a shell wrapper that uses the same Python as the current process
    # and invokes the bazel_wrapper module. Using -m ensures the package is found
    # whether installed via wheel or running from source with PYTHONPATH.
    wrapper_script = f"""#!/bin/sh
exec "{sys.executable}" -m tools.claude_hooks.bazel_wrapper "$@"
"""
    wrapper_path.write_text(wrapper_script)
    wrapper_path.chmod(0o755)
    logger.info("Installed bazel wrapper at %s with health checks", wrapper_path)

    # Create bazelisk symlink for pre-commit hooks
    bazelisk_symlink = wrapper_dir / "bazelisk"
    if bazelisk_symlink.exists() or bazelisk_symlink.is_symlink():
        bazelisk_symlink.unlink()
    bazelisk_symlink.symlink_to(wrapper_path)
    logger.info("Created bazelisk symlink at %s", bazelisk_symlink)

    return wrapper_path
