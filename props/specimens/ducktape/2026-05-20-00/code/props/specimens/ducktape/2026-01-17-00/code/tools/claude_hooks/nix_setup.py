"""Nix installation and tool setup for Claude Code web sessions."""

from __future__ import annotations

import logging
import os
import shutil
import urllib.request
from pathlib import Path

from claude_hooks.resources import CONFIG_FILES
from claude_hooks.streaming import run_streaming

logger = logging.getLogger(__name__)

# Shared cache directory for Claude Code web hooks
HOOK_CACHE_DIR = Path.home() / ".cache" / "claude-code-web"


def find_nix_bin() -> Path | None:
    """Find nix binary directory in /nix/store."""
    nix_store = Path("/nix/store")
    if not nix_store.exists():
        return None
    for entry in sorted(nix_store.iterdir(), reverse=True):
        if "-nix-" in entry.name:
            bin_dir = entry / "bin"
            if bin_dir.exists() and (bin_dir / "nix").exists():
                return bin_dir
    return None


def get_nix_paths(nix_store_bin: Path) -> list[Path]:
    """Get list of nix-related paths to add to PATH."""
    paths = [nix_store_bin, Path.home() / ".nix-profile" / "bin"]
    return [p for p in paths if p.exists()]


def setup_nix_path(nix_store_bin: Path) -> None:
    """Add nix store bin and profile bin to os.environ PATH."""
    paths = get_nix_paths(nix_store_bin)
    if paths:
        os.environ["PATH"] = ":".join(map(str, paths)) + ":" + os.environ.get("PATH", "")
        logger.info("Added to PATH: %s", ", ".join(map(str, paths)))


def _write_nix_conf() -> Path:
    """Write nix.conf to shared cache directory, return path."""
    HOOK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    nix_conf_path = HOOK_CACHE_DIR / "nix.conf"

    nix_conf_content = CONFIG_FILES.joinpath("nix.conf").read_text()
    nix_conf_path.write_text(nix_conf_content)

    return nix_conf_path


NIX_INSTALL_SCRIPT = Path("/tmp/nix-install.sh")


def install_nix() -> Path:
    """Install nix if not present. Returns the nix store bin path."""
    # Write nix.conf to shared cache directory
    nix_conf = _write_nix_conf()
    os.environ["NIX_USER_CONF_FILES"] = str(nix_conf)
    logger.info("Using nix.conf: %s", nix_conf)

    # Check if nix is already in the store
    nix_store_bin = find_nix_bin()
    if not nix_store_bin:
        logger.info("Installing nix...")
        with urllib.request.urlopen("https://nixos.org/nix/install", timeout=60) as response:
            NIX_INSTALL_SCRIPT.write_bytes(response.read())

        # The nix-env step fails in gVisor containers due to a PTY bug.
        # nix-env opens /dev/ptmx, forks a sandbox process, then reads from the PTY master.
        # gVisor returns EIO on this read (race condition in PTY emulation).
        #
        # ROOT CAUSE (discovered via strace):
        # Claude Code web runs on gVisor (runsc), not a real Linux kernel. gVisor's PTY
        # emulation has a race condition. When nix-env builds a derivation, it:
        #   1. Opens /dev/ptmx to create a PTY pair (master fd)
        #   2. Forks a child process for the build sandbox
        #   3. Parent immediately calls read() on the PTY master
        #   4. gVisor returns EIO instead of blocking until data arrives
        #
        # WORKAROUND:
        # Skip nix-env entirely. The installer already unpacked Nix to /nix/store.
        # We use the store path directly instead of relying on profiles.
        run_streaming(
            ["sh", "-x", NIX_INSTALL_SCRIPT, "--no-daemon", "--no-channel-add", "--no-modify-profile"],
            check=False,  # Installer may fail on nix-env step, that's OK
        )

        nix_store_bin = find_nix_bin()
        if not nix_store_bin:
            raise RuntimeError("Failed to install nix - no nix binary found in store")

    else:
        logger.info("nix already in store: %s", nix_store_bin)

    logger.info("nix installed: %s", nix_store_bin)
    setup_nix_path(nix_store_bin)
    return nix_store_bin


def install_tools(nix_store_bin: Path, tools: list[str]) -> None:
    """Install tools via nix profile using the store path directly.

    Uses the nix binary from the store path, NOT from PATH or profile.
    This avoids the issue where `nix profile install` replaces the profile
    and removes nix from PATH.
    """
    # Filter out tools that are already available
    missing_tools = [t for t in tools if not shutil.which(t)]
    if not missing_tools:
        logger.info("All tools already available: %s", ", ".join(tools))
        return

    logger.info("Installing tools: %s", missing_tools)

    run_streaming(
        [
            nix_store_bin / "nix",
            "profile",
            "install",
            "-v",
            "--print-build-logs",
            *[f"nixpkgs#{t}" for t in missing_tools],
        ]
    )

    logger.info("Tools installed successfully")
