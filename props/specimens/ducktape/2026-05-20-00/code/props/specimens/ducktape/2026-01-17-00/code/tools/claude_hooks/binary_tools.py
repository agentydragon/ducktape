"""Install binary tools via direct download.

Binary downloads are used instead of nix because nix setup is too slow
and times out in the Claude Code web environment.

IMPORTANT: This module must not import any non-stdlib packages.
"""

from __future__ import annotations

import io
import logging
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Install location
TOOLS_DIR = Path.home() / ".local" / "bin"


@dataclass
class Tool:
    """Binary tool definition."""

    name: str  # Display name and default binary name
    version: str
    url_builder: Callable[[str, str], str]  # (version, arch) -> URL
    binary: str = field(default="")  # Binary name (defaults to name if empty)
    is_archive: bool = True  # True for .tar.gz/.zip, False for plain binary

    def __post_init__(self) -> None:
        if not self.binary:
            self.binary = self.name


# Cluster tools for pre-commit hooks (terraform, k8s, etc.)
CLUSTER_TOOLS = [
    Tool(
        name="opentofu",
        binary="tofu",  # Binary differs from name
        version="1.9.0",
        url_builder=lambda v, a: f"https://github.com/opentofu/opentofu/releases/download/v{v}/tofu_{v}_linux_{a}.zip",
    ),
    Tool(
        name="tflint",
        version="0.53.0",
        url_builder=lambda v,
        a: f"https://github.com/terraform-linters/tflint/releases/download/v{v}/tflint_linux_{a}.zip",
    ),
    Tool(
        name="flux",
        version="2.4.0",
        url_builder=lambda v, a: f"https://github.com/fluxcd/flux2/releases/download/v{v}/flux_{v}_linux_{a}.tar.gz",
    ),
    Tool(
        name="kustomize",
        version="5.5.0",
        url_builder=lambda v,
        a: f"https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv{v}/kustomize_v{v}_linux_{a}.tar.gz",
    ),
    Tool(
        name="kubeseal",
        version="0.27.3",
        url_builder=lambda v,
        a: f"https://github.com/bitnami-labs/sealed-secrets/releases/download/v{v}/kubeseal-{v}-linux-{a}.tar.gz",
    ),
    Tool(name="helm", version="3.16.4", url_builder=lambda v, a: f"https://get.helm.sh/helm-v{v}-linux-{a}.tar.gz"),
]


def _alejandra_arch(arch: str) -> str:
    """Convert normalized arch to alejandra's naming convention."""
    return "x86_64" if arch == "amd64" else "aarch64"


# Development tools
DEV_TOOLS = [
    Tool(
        name="alejandra",
        version="4.0.0",
        url_builder=lambda v,
        a: f"https://github.com/kamadorueda/alejandra/releases/download/{v}/alejandra-{_alejandra_arch(a)}-unknown-linux-musl",
        is_archive=False,
    )
]


def _get_arch() -> str:
    """Get normalized architecture name."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(f"Unsupported architecture: {machine}")


def _install_binary(content: bytes, binary_name: str, dest_path: Path) -> None:
    """Install binary content to dest_path."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)
    dest_path.chmod(0o755)
    logger.info("Installed %s to %s", binary_name, dest_path)


def _download_plain_binary(url: str, binary_name: str, dest_path: Path) -> None:
    """Download a plain binary file directly to dest_path."""
    logger.info("Downloading %s from %s", binary_name, url)

    with urllib.request.urlopen(url, timeout=120) as response:
        _install_binary(response.read(), binary_name, dest_path)


def _download_and_extract(url: str, binary_name: str, dest_path: Path) -> None:
    """Download archive and extract binary directly to dest_path.

    Streams directly from URL, extracts only the target binary.

    Raises:
        RuntimeError: If binary not found in archive or download fails.
    """
    logger.info("Downloading %s from %s", binary_name, url)

    with urllib.request.urlopen(url, timeout=120) as response:
        if url.endswith((".tar.gz", ".tgz")):
            with tarfile.open(fileobj=response, mode="r:gz") as tar:
                for member in tar:
                    if member.name.endswith(binary_name) and member.isfile():
                        extracted = tar.extractfile(member)
                        if extracted:
                            _install_binary(extracted.read(), binary_name, dest_path)
                            return
        elif url.endswith(".zip"):
            # ZipFile needs seekable file, must buffer
            data = io.BytesIO(response.read())
            with zipfile.ZipFile(data) as zf:
                for name in zf.namelist():
                    if name.endswith(binary_name):
                        _install_binary(zf.read(name), binary_name, dest_path)
                        return
        else:
            raise RuntimeError(f"Unknown archive format: {url}")

    raise RuntimeError(f"Binary {binary_name} not found in archive {url}")


def _is_installed(binary_name: str, version_flag: str = "--version") -> bool:
    """Check if a tool is already installed and working."""
    binary_path = TOOLS_DIR / binary_name
    # Check if binary exists in our tools dir or is in PATH
    if not binary_path.exists() and shutil.which(binary_name) is None:
        return False
    try:
        result = subprocess.run([binary_name, version_flag], capture_output=True, text=True, check=False, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _install_tool(tool: Tool) -> None:
    """Install a single tool if not already installed.

    Raises:
        RuntimeError: If installation fails.
    """
    if _is_installed(tool.binary):
        logger.info("%s already installed", tool.name)
        return

    url = tool.url_builder(tool.version, _get_arch())
    dest_path = TOOLS_DIR / tool.binary
    if tool.is_archive:
        _download_and_extract(url, tool.binary, dest_path)
    else:
        _download_plain_binary(url, tool.binary, dest_path)


def install_tools(tools: list[Tool]) -> None:
    """Install a list of tools.

    Raises:
        RuntimeError: If any tool fails to install.
    """
    for tool in tools:
        _install_tool(tool)


def install_cluster_tools() -> None:
    """Install cluster tools for pre-commit hooks."""
    install_tools(CLUSTER_TOOLS)


def install_dev_tools() -> None:
    """Install development tools (alejandra, etc.)."""
    install_tools(DEV_TOOLS)


def get_cluster_tools_status() -> str:
    """Get status string for cluster tools."""
    return _get_tools_status(CLUSTER_TOOLS)


def get_dev_tools_status() -> str:
    """Get status string for dev tools."""
    return _get_tools_status(DEV_TOOLS)


def _get_tools_status(tools: list[Tool]) -> str:
    """Get status string for a list of tools."""
    all_names = {t.name for t in tools}
    installed = {t.name for t in tools if _is_installed(t.binary)}
    missing = all_names - installed

    if not missing:
        return f"all installed ({', '.join(sorted(installed))})"
    if not installed:
        return "none installed"
    return f"partial ({', '.join(sorted(installed))}; missing: {', '.join(sorted(missing))})"
