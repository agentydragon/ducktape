"""Install mkcert and generate a trusted localhost TLS certificate.

mkcert creates a local Certificate Authority and generates certificates
signed by that CA. This gives localhost a valid TLS certificate trusted
by curl, Python, Node, etc. via the combined CA bundle (SSL_CERT_FILE).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import httpx
from opentelemetry import trace

from tools.claude_hooks.platform_utils import get_platform
from tools.claude_hooks.settings import HookSettings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MKCERT_VERSION = "1.4.4"


@dataclass
class MkcertSetup:
    """Result of mkcert installation and certificate generation."""

    cert_path: Path
    key_path: Path
    ca_root: Path
    status: str


def _get_mkcert_dir(settings: HookSettings) -> Path:
    return settings.get_mkcert_dir()


def _get_mkcert_binary(settings: HookSettings) -> Path:
    return settings.get_mkcert_binary()


def _get_download_url() -> str:
    """Get the appropriate mkcert download URL for this platform."""
    p = get_platform()
    return (
        f"https://github.com/FiloSottile/mkcert/releases/download/"
        f"v{MKCERT_VERSION}/mkcert-v{MKCERT_VERSION}-{p.system}-{p.arch}"
    )


async def _download_mkcert(settings: HookSettings) -> Path:
    """Download mkcert binary if not already present."""
    mkcert_dir = _get_mkcert_dir(settings)
    mkcert_path = _get_mkcert_binary(settings)

    mkcert_dir.mkdir(parents=True, exist_ok=True)

    if mkcert_path.exists():
        logger.info("mkcert already downloaded: %s", mkcert_path)
        return mkcert_path

    url = _get_download_url()
    logger.info("Downloading mkcert from %s", url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content

    mkcert_path.write_bytes(data)
    mkcert_path.chmod(mkcert_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed mkcert to %s", mkcert_path)
    return mkcert_path


def append_mkcert_ca_to_bundle(ca_root: Path, combined_ca: Path) -> None:
    """Append mkcert root CA to the combined CA bundle.

    The combined CA bundle is recreated from scratch on each hook run
    (system CAs + proxy CA), so we always need to re-append.
    """
    root_ca = ca_root / "rootCA.pem"
    if not root_ca.exists():
        logger.warning("mkcert root CA not found at %s, skipping bundle append", root_ca)
        return
    ca_content = root_ca.read_text()
    with combined_ca.open("a") as f:
        f.write("\n# mkcert local CA\n")
        f.write(ca_content)
    logger.info("Appended mkcert root CA to %s", combined_ca)


async def setup_mkcert(settings: HookSettings, combined_ca: Path | None) -> MkcertSetup:
    """Generate a trusted localhost TLS certificate via mkcert.

    Downloads the mkcert binary if needed, generates a certificate for
    localhost/127.0.0.1/::1, and optionally appends the root CA to the
    combined CA bundle so Python/curl/Node trust it via SSL_CERT_FILE.

    Note: `mkcert -install` (system trust store installation) is intentionally
    skipped. We rely on the combined CA bundle (SSL_CERT_FILE) for tool trust,
    which doesn't require browser or OS trust store integration. mkcert
    automatically creates rootCA.pem in CAROOT when generating the first cert,
    so -install is not needed to produce the CA file.
    """
    mkcert_dir = _get_mkcert_dir(settings)
    mkcert_dir.mkdir(parents=True, exist_ok=True)

    ca_root = mkcert_dir / "ca"
    ca_root.mkdir(parents=True, exist_ok=True)

    cert_path = mkcert_dir / "localhost.pem"
    key_path = mkcert_dir / "localhost-key.pem"

    env = {**os.environ, "CAROOT": str(ca_root)}

    if not cert_path.exists() or not key_path.exists():
        with tracer.start_as_current_span("mkcert_download_binary"):
            mkcert_bin = await _download_mkcert(settings)

        logger.info("Generating localhost certificate...")
        with tracer.start_as_current_span("mkcert_generate_cert"):
            proc = await asyncio.create_subprocess_exec(
                mkcert_bin,
                "-cert-file",
                cert_path,
                "-key-file",
                key_path,
                "localhost",
                "127.0.0.1",
                "::1",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"mkcert certificate generation failed: {stderr.decode().strip()}")
        logger.info("Generated localhost cert: %s", cert_path)

    with tracer.start_as_current_span("mkcert_append_bundle"):
        if combined_ca and combined_ca.exists():
            append_mkcert_ca_to_bundle(ca_root, combined_ca)

    return MkcertSetup(cert_path=cert_path, key_path=key_path, ca_root=ca_root, status=f"installed ({cert_path})")
