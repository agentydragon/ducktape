"""Install Bazelisk for Bazel version management.

Bazelisk automatically downloads and runs the correct Bazel version
based on .bazelversion or USE_BAZEL_VERSION.

TODO: Eventually unify tool installation via direnv/devenv instead of
      manual downloads in session hooks.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
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

    if system not in ("linux", "darwin"):
        raise RuntimeError(f"Unsupported OS: {system}")

    binary = f"bazelisk-{system}-{arch}"
    return f"https://github.com/bazelbuild/bazelisk/releases/download/v{BAZELISK_VERSION}/{binary}"


def install_bazelisk(settings: HookSettings) -> Path:
    """Download bazelisk to private location, returning the binary path.

    Installs to ~/.cache/claude-hooks/auth-proxy/bazelisk (private, not on PATH).
    The wrapper script in ~/.cache/claude-hooks/auth-proxy/bin/bazel will call this.
    Skips download if already installed.
    """
    auth_proxy_dir = settings.get_auth_proxy_dir()
    bazelisk_path = settings.get_bazelisk_path()

    auth_proxy_dir.mkdir(parents=True, exist_ok=True)

    # Check if already installed
    if get_bazelisk_version(settings):
        logger.info("Bazelisk already installed: %s", bazelisk_path)
        return bazelisk_path

    url = get_bazelisk_url()
    logger.info("Downloading Bazelisk from %s", url)

    # urllib respects HTTPS_PROXY env var and uses system CA bundle (SSL_CERT_FILE).
    # In CC web, system CAs already include the Anthropic TLS inspection CA.
    with urllib.request.urlopen(url, timeout=60) as response:
        bazelisk_path.write_bytes(response.read())

    # Make executable
    bazelisk_path.chmod(bazelisk_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed Bazelisk to %s", bazelisk_path)

    return bazelisk_path


def install_wrapper(settings: HookSettings) -> Path:
    """Install wrapper script that sets proxy env vars before calling bazelisk.

    The wrapper is in ~/.cache/claude-hooks/auth-proxy/bin/bazel and calls the real
    bazelisk at ~/.cache/claude-hooks/auth-proxy/bazelisk.
    Also creates a bazelisk symlink for pre-commit hooks.
    Includes health checks for supervisor and proxy service.

    The wrapper reads configuration from environment variables (set via get_env_script).

    Uses sys.executable -m to invoke the bazel_wrapper module. This works in both:
    - Bazel mode: PYTHONPATH is baked into the wrapper script at install time
    - Wheel mode: The package is installed in the Python environment
    """
    wrapper_dir = settings.get_wrapper_dir()
    wrapper_path = settings.get_wrapper_path()

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    # Use sys.executable -m for both Bazel and wheel mode.
    # Bake PYTHONPATH into the script so the wrapper can find dependencies (e.g. PyJWT)
    # without leaking it into every agent subprocess via the env file.
    # In wheel mode PYTHONPATH is unset, so no export line is emitted.
    pythonpath = os.environ.get("PYTHONPATH")
    pythonpath_line = f'\nexport PYTHONPATH="{pythonpath}"' if pythonpath else ""
    wrapper_script = f"""#!/bin/sh{pythonpath_line}
exec "{sys.executable}" -m tools.claude_hooks.bazel_wrapper "$@"
"""
    logger.info("Installed bazel wrapper at %s (using %s)", wrapper_path, sys.executable)

    wrapper_path.write_text(wrapper_script)
    wrapper_path.chmod(0o755)

    # Create bazelisk symlink for pre-commit hooks
    bazelisk_symlink = wrapper_dir / "bazelisk"
    if bazelisk_symlink.exists() or bazelisk_symlink.is_symlink():
        bazelisk_symlink.unlink()
    bazelisk_symlink.symlink_to(wrapper_path)
    logger.info("Created bazelisk symlink at %s", bazelisk_symlink)

    return wrapper_path
