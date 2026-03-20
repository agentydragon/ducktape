"""Install CLI tools (gh, kubectl, flux) into the session bin dir.

Downloads standalone static binaries during web-mode session start.
Each tool is installed independently — failures are logged but don't block
the session or other tool installs.

Follows the same pattern as bazelisk_setup.py (http_client, platform_utils).

TODO(unify-web-cli): Make tool installation truly async — have session_start
exit before downloads complete, so the hook returns quickly. The bin dir is
already on PATH, so tools will appear as downloads finish. Needs a background
daemon or subprocess approach (not just asyncio.gather in-process).
"""

import logging
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx

from devinfra.claude.http_client import download
from devinfra.claude.platform_utils import get_platform

logger = logging.getLogger(__name__)

_GH_VERSION = "2.67.0"
_KUBECTL_VERSION = "v1.32.3"
_FLUX_VERSION = "2.4.0"


@dataclass(frozen=True)
class CliTool:
    """A CLI tool to install."""

    name: str
    url: str
    # If the download is a tarball, extract this member (relative path in archive).
    # None means the download is the raw binary.
    tar_member: str | None = None


def _tools() -> list[CliTool]:
    """Return the list of tools to install, resolved for the current platform."""
    p = get_platform()
    return [
        CliTool(
            name="gh",
            url=(
                f"https://github.com/cli/cli/releases/download/v{_GH_VERSION}/"
                f"gh_{_GH_VERSION}_{p.system}_{p.arch}.tar.gz"
            ),
            tar_member=f"gh_{_GH_VERSION}_{p.system}_{p.arch}/bin/gh",
        ),
        CliTool(name="kubectl", url=f"https://dl.k8s.io/release/{_KUBECTL_VERSION}/bin/{p.system}/{p.arch}/kubectl"),
        CliTool(
            name="flux",
            url=(
                f"https://github.com/fluxcd/flux2/releases/download/v{_FLUX_VERSION}/"
                f"flux_{_FLUX_VERSION}_{p.system}_{p.arch}.tar.gz"
            ),
            tar_member="flux",
        ),
    ]


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_tool(tool: CliTool, bin_dir: Path, http: httpx.Client) -> None:
    """Download and install a single CLI tool (synchronous)."""
    dest = bin_dir / tool.name
    if dest.exists():
        logger.debug("%s already installed at %s", tool.name, dest)
        return

    if shutil.which(tool.name):
        logger.debug("%s already on PATH, skipping download", tool.name)
        return

    logger.info("Downloading %s from %s", tool.name, tool.url)

    data = download(tool.url, http)

    if tool.tar_member:
        with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tf:
            member = tf.getmember(tool.tar_member)
            f = tf.extractfile(member)
            if f is None:
                raise RuntimeError(f"Tar member {tool.tar_member} is not a file")
            data = f.read()

    # Atomic write: temp file → chmod → rename
    with tempfile.NamedTemporaryFile(dir=bin_dir, delete=False, prefix=f".{tool.name}.") as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    _make_executable(tmp_path)
    tmp_path.rename(dest)
    logger.info("Installed %s to %s", tool.name, dest)


def install_cli_tools(bin_dir: Path, http: httpx.Client, *, skip: set[str] | None = None) -> list[str]:
    """Install all CLI tools into bin_dir. Returns list of successfully installed tool names.

    Non-fatal — logs warnings for individual failures and continues.
    skip: tool names to skip (e.g. {"gh", "kubectl", "flux"}).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for tool in _tools():
        if skip and tool.name in skip:
            logger.info("Skipping %s install (disabled by settings)", tool.name)
            continue
        try:
            _install_tool(tool, bin_dir, http)
            installed.append(tool.name)
        except Exception:
            logger.warning("Failed to install %s", tool.name, exc_info=True)
    return installed
