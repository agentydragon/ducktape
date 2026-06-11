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
import stat
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from mako.template import Template

from claude_hooks import proxy_setup
from claude_hooks.resources import CONFIG_FILES

logger = logging.getLogger(__name__)

BAZELISK_VERSION = "1.25.0"


def _get_bazelisk_path() -> Path:
    """Get the bazelisk binary path."""
    return proxy_setup._get_bazel_proxy_dir() / "bazelisk"


def _get_wrapper_dir() -> Path:
    """Get the wrapper directory (added to PATH)."""
    return proxy_setup._get_bazel_proxy_dir() / "bin"


def _get_wrapper_path() -> Path:
    """Get the wrapper script path."""
    return _get_wrapper_dir() / "bazel"


def get_bazelisk_version() -> str | None:
    """Get bazelisk version string, or None if not installed/working."""
    bazelisk_path = _get_bazelisk_path()
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


def install_bazelisk() -> Path:
    """Download bazelisk to private location, returning the binary path.

    Installs to ~/.cache/bazel-proxy/bazelisk (private, not on PATH).
    The wrapper script in ~/.cache/bazel-proxy/bin/bazel will call this.
    Skips download if already installed.
    """
    bazel_proxy_dir = proxy_setup._get_bazel_proxy_dir()
    bazelisk_path = _get_bazelisk_path()

    bazel_proxy_dir.mkdir(parents=True, exist_ok=True)

    # Check if already installed
    if get_bazelisk_version():
        logger.info("Bazelisk already installed: %s", bazelisk_path)
        return bazelisk_path

    url = get_bazelisk_url()
    logger.info("Downloading Bazelisk from %s", url)

    # Download with proxy support (urllib respects https_proxy env var)
    with urllib.request.urlopen(url, timeout=60) as response:
        bazelisk_path.write_bytes(response.read())

    # Make executable
    bazelisk_path.chmod(bazelisk_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed Bazelisk to %s", bazelisk_path)

    return bazelisk_path


def install_wrapper() -> Path:
    """Install wrapper script that sets proxy env vars before calling bazelisk.

    The wrapper is in ~/.cache/bazel-proxy/bin/bazel and calls the real
    bazelisk at ~/.cache/bazel-proxy/bazelisk.
    Also creates a bazelisk symlink for pre-commit hooks.
    Includes health checks for supervisor and proxy service.

    The wrapper reads configuration from environment variables (set via get_env_script).
    """
    wrapper_dir = _get_wrapper_dir()
    wrapper_path = _get_wrapper_path()

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    # Create a shell wrapper that uses the same Python as the current process
    # and invokes the bazel_wrapper module. Using -m ensures the package is found
    # whether installed via wheel or running from source with PYTHONPATH.
    wrapper_script = f"""#!/bin/sh
exec "{sys.executable}" -m claude_hooks.bazel_wrapper "$@"
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


def get_env_script(
    proxy_port: int, repo_root: Path, hook_timestamp: datetime, combined_ca: Path, nix_paths: list[Path] | None = None
) -> str:
    """Get bash script fragment to add wrapper dir to PATH and set config env vars.

    This should be written to CLAUDE_ENV_FILE.

    Args:
        proxy_port: Port for the local Bazel proxy
        repo_root: Path to the repository root for error messages
        hook_timestamp: Session start hook timestamp
        combined_ca: Path to combined CA bundle for Node.js
        nix_paths: List of nix-related paths to add to PATH (optional)
    """
    local_proxy = f"http://localhost:{proxy_port}"

    exports = {
        "BAZELISK_PATH": _get_bazelisk_path(),
        "BAZEL_LOCAL_PROXY": local_proxy,
        "BAZEL_SYSTEM_BAZELRC_PATH": proxy_setup._get_bazel_proxy_rc(),
        "DUCKTAPE_REPO_ROOT": repo_root,
        "DUCKTAPE_SESSION_START_HOOK_TS": hook_timestamp.isoformat(),
        # Props e2e test configuration (podman + host networking)
        "PGHOST": "127.0.0.1",
        "PGPORT": "5433",
        "AGENT_PGHOST": "127.0.0.1",
        "PROPS_REGISTRY_PROXY_HOST": "127.0.0.1",
        "PROPS_REGISTRY_PROXY_PORT": "5051",
        "PROPS_DOCKER_NETWORK": "host",
        "NODE_EXTRA_CA_CERTS": combined_ca,
    }

    # Build list of paths to prepend to PATH (wrapper dir first, then nix paths)
    prepend_paths = [_get_wrapper_dir(), *(nix_paths or [])]

    # Render env script from template
    # Pass shlex.quote as 'sh' filter for shell escaping
    template = Template(CONFIG_FILES.joinpath("env.mako").read_text(), imports=["from shlex import quote as sh"])
    result: str = template.render(prepend_paths=prepend_paths, exports=exports)
    return result


def get_status() -> str:
    """Get status string for logging."""
    version = get_bazelisk_version()
    if not version:
        return "not installed"

    wrapper_path = _get_wrapper_path()
    bazel_on_path = shutil.which("bazel")
    if bazel_on_path and Path(bazel_on_path).resolve() == wrapper_path.resolve():
        return f"{version} ({wrapper_path})"
    if wrapper_path.exists():
        return f"{version} (wrapper exists but not on PATH)"
    return f"{version} (no wrapper)"
