"""Generate a trusted localhost TLS certificate via mkcert.

mkcert creates a local Certificate Authority and generates certificates
signed by that CA. This gives localhost a valid TLS certificate trusted
by curl, Python, Node, etc. via the combined CA bundle (SSL_CERT_FILE).

The mkcert binary is provided by Nix via the web-session package.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from devinfra.claude.session_paths import SessionPaths

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class MkcertSetup:
    """Result of mkcert installation and certificate generation."""

    cert_path: Path
    key_path: Path
    ca_root: Path
    status: str


def _get_mkcert_dir(paths: SessionPaths) -> Path:
    return paths.mkcert_dir


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


async def setup_mkcert(paths: SessionPaths, combined_ca: Path | None) -> MkcertSetup:
    """Generate a trusted localhost TLS certificate via mkcert.

    Uses the mkcert binary from PATH (provided by Nix web-session package).
    Generates a certificate for localhost/127.0.0.1/::1, and optionally
    appends the root CA to the combined CA bundle so Python/curl/Node trust
    it via SSL_CERT_FILE.
    """
    mkcert_dir = _get_mkcert_dir(paths)
    mkcert_dir.mkdir(parents=True, exist_ok=True)

    ca_root = mkcert_dir / "ca"
    ca_root.mkdir(parents=True, exist_ok=True)

    cert_path = mkcert_dir / "localhost.pem"
    key_path = mkcert_dir / "localhost-key.pem"

    env = {**os.environ, "CAROOT": str(ca_root)}

    if not cert_path.exists() or not key_path.exists():
        mkcert_bin = shutil.which("mkcert")
        if not mkcert_bin:
            raise RuntimeError("mkcert not found on PATH (expected from Nix web-session package)")

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
