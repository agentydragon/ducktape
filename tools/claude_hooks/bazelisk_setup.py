"""Install Bazelisk for Bazel version management.

Bazelisk automatically downloads and runs the correct Bazel version
based on .bazelversion or USE_BAZEL_VERSION.

TODO: Eventually unify tool installation via direnv/devenv instead of
      manual downloads in session hooks.
"""

from __future__ import annotations

import logging
import shutil
import stat
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bazel_util.subprocess import write_shell_wrapper
from tools.claude_hooks.platform_utils import get_platform
from tools.claude_hooks.settings import ENV_SESSION_DIR, HookSettings

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
    # Use --version (a bazelisk flag) instead of "version" (a bazel subcommand).
    # "bazel version" starts a Bazel server, which is expensive and may start
    # without JVM proxy args if called outside the wrapper.
    result = subprocess.run([bazelisk_path, "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    # Return first line (e.g. "Bazelisk version: v1.25.0")
    return result.stdout.split("\n")[0].strip()


def get_bazelisk_url() -> str:
    """Get the appropriate Bazelisk download URL for this platform."""
    p = get_platform()
    binary = f"bazelisk-{p.system}-{p.arch}"
    return f"https://github.com/bazelbuild/bazelisk/releases/download/v{BAZELISK_VERSION}/{binary}"


def install_bazelisk(settings: HookSettings) -> Path:
    """Download bazelisk to private location, returning the binary path.

    Installs to ~/.cache/claude-hooks/auth-proxy/bazelisk (private, not on PATH).
    The wrapper script in ~/.cache/claude-hooks/auth-proxy/bin/bazel will call this.
    Skips download if already installed.
    """
    bazelisk_path = settings.get_bazelisk_path()
    bazelisk_path.parent.mkdir(parents=True, exist_ok=True)

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


_WRAPPER_RUNTIME_LINES = (
    'export _BAZEL_WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd)"\nexport _BAZEL_WRAPPER_NAME="$(basename "$0")"'
)


def install_wrapper(settings: HookSettings, *, wrapper_dir: Path | None = None) -> Path:
    """Install wrapper script that sets proxy env vars before calling bazelisk.

    The wrapper is in ~/.cache/claude-hooks/auth-proxy/bin/bazel (web mode) or a
    session-specific bin directory (CLI mode). Also creates a bazelisk symlink.

    Bakes DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR into the script so subprocesses
    (pre-commit, CI) that don't source the env file still have the session dir set.

    Args:
        settings: Hook settings
        wrapper_dir: Optional directory for wrappers (defaults to settings.get_wrapper_dir())
    """
    if wrapper_dir is None:
        wrapper_dir = settings.get_wrapper_dir()
    wrapper_path = wrapper_dir / "bazel"

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    write_shell_wrapper(
        wrapper_path,
        "tools.claude_hooks.bazel_wrapper",
        baked_env={ENV_SESSION_DIR: str(settings.session_dir)},
        extra_lines=_WRAPPER_RUNTIME_LINES,
    )
    logger.info("Installed bazel wrapper at %s", wrapper_path)

    # Create bazelisk symlink for pre-commit hooks
    bazelisk_symlink = wrapper_dir / "bazelisk"
    if bazelisk_symlink.exists() or bazelisk_symlink.is_symlink():
        bazelisk_symlink.unlink()
    bazelisk_symlink.symlink_to(wrapper_path)
    logger.info("Created bazelisk symlink at %s", bazelisk_symlink)

    return wrapper_path
