"""Install mkcert and generate a trusted localhost TLS certificate.

mkcert creates a local Certificate Authority and installs it into system
trust stores, then generates certificates signed by that CA. This gives
localhost a valid TLS certificate trusted by curl, Python, Node, etc.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tools.claude_hooks.platform_utils import get_platform
from tools.claude_hooks.settings import HookSettings

logger = logging.getLogger(__name__)

MKCERT_VERSION = "1.4.4"


@dataclass
class MkcertSetup:
    """Result of mkcert installation and certificate generation."""

    cert_path: Path
    key_path: Path
    ca_root: Path
    status: str


def _get_mkcert_dir(settings: HookSettings) -> Path:
    return settings.get_cache_dir() / "mkcert"


def _get_mkcert_binary(settings: HookSettings) -> Path:
    return _get_mkcert_dir(settings) / "mkcert"


def _get_download_url() -> str:
    """Get the appropriate mkcert download URL for this platform."""
    p = get_platform()
    return (
        f"https://github.com/FiloSottile/mkcert/releases/download/"
        f"v{MKCERT_VERSION}/mkcert-v{MKCERT_VERSION}-{p.system}-{p.arch}"
    )


def _download_mkcert(settings: HookSettings) -> Path:
    """Download mkcert binary if not already present."""
    mkcert_dir = _get_mkcert_dir(settings)
    mkcert_path = _get_mkcert_binary(settings)

    mkcert_dir.mkdir(parents=True, exist_ok=True)

    if mkcert_path.exists():
        logger.info("mkcert already downloaded: %s", mkcert_path)
        return mkcert_path

    url = _get_download_url()
    logger.info("Downloading mkcert from %s", url)

    with urllib.request.urlopen(url, timeout=60) as response:
        mkcert_path.write_bytes(response.read())

    mkcert_path.chmod(mkcert_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed mkcert to %s", mkcert_path)
    return mkcert_path


def _append_ca_to_bundle(root_ca: Path, combined_ca: Path) -> None:
    """Append mkcert root CA to the combined CA bundle.

    The combined CA bundle is recreated from scratch on each hook run
    (system CAs + proxy CA), so we always need to re-append.
    """
    ca_content = root_ca.read_text()
    with combined_ca.open("a") as f:
        f.write("\n# mkcert local CA\n")
        f.write(ca_content)
    logger.info("Appended mkcert root CA to %s", combined_ca)


def setup_mkcert(settings: HookSettings, combined_ca: Path | None) -> MkcertSetup:
    """Install mkcert, generate trusted localhost certificate.

    Downloads mkcert, installs a local CA into system trust stores,
    generates a certificate for localhost/127.0.0.1/::1, and appends
    the root CA to the combined CA bundle so Python/curl/Node trust it.
    """
    mkcert_dir = _get_mkcert_dir(settings)
    mkcert_dir.mkdir(parents=True, exist_ok=True)

    ca_root = mkcert_dir / "ca"
    ca_root.mkdir(parents=True, exist_ok=True)

    cert_path = mkcert_dir / "localhost.pem"
    key_path = mkcert_dir / "localhost-key.pem"

    env = {**os.environ, "CAROOT": str(ca_root)}

    if not cert_path.exists() or not key_path.exists():
        mkcert_bin = _download_mkcert(settings)

        # Install local CA into system trust stores
        logger.info("Installing mkcert local CA (CAROOT=%s)...", ca_root)
        result = subprocess.run([str(mkcert_bin), "-install"], env=env, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            # -install can fail if certutil (libnss3-tools) is missing;
            # the CA still works for non-browser tools via the combined bundle
            logger.warning("mkcert -install returned %d: %s", result.returncode, result.stderr.strip())
        else:
            logger.info("mkcert CA installed into system trust stores")

        # Generate localhost certificate
        logger.info("Generating localhost certificate...")
        subprocess.run(
            [
                str(mkcert_bin),
                "-cert-file",
                str(cert_path),
                "-key-file",
                str(key_path),
                "localhost",
                "127.0.0.1",
                "::1",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Generated localhost cert: %s", cert_path)

    # Append mkcert root CA to combined CA bundle so Python/curl/Node trust localhost
    root_ca = ca_root / "rootCA.pem"
    if combined_ca and combined_ca.exists() and root_ca.exists():
        _append_ca_to_bundle(root_ca, combined_ca)

    return MkcertSetup(cert_path=cert_path, key_path=key_path, ca_root=ca_root, status=f"installed ({cert_path})")
