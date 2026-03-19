"""Auth proxy setup for Claude Code web's TLS-inspecting proxy.

Handles:
- Writing credentials for the in-process auth proxy
- Loading the Anthropic TLS inspection CA certificate from the filesystem
- Creating a Java truststore with the CA for Bazel
- Creating combined CA bundle for SSL tools
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from opentelemetry import trace

from devinfra.claude.auth_proxy.proxy import AuthForwardingProxy
from devinfra.claude.auth_proxy.vars import get_upstream_proxy_url
from devinfra.claude.errors import CaBundleError, CaExtractionError, ProxyServiceError, TruststoreError
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings
from util.net import async_wait_for_port

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Env vars set by Bazel BUILD targets (rlocation keys for hermetic JDK files)
_KEYTOOL_RLOCATION_ENV = "KEYTOOL_RLOCATION"
_JAVA_CACERTS_RLOCATION_ENV = "JAVA_CACERTS_RLOCATION"

# Pre-installed Anthropic CA on Claude Code web containers
ANTHROPIC_CA_PREINSTALLED = Path("/usr/local/share/ca-certificates/swp-ca-production.crt")

# Expected CA certificate attributes for Anthropic TLS inspection CA
ANTHROPIC_CA_ORG = "Anthropic"
ANTHROPIC_CA_CN_SUBSTRING = "TLS Inspection CA"

# Java truststore password (standard default)
TRUSTSTORE_PASSWORD = "changeit"

# System file locations with fallbacks
SYSTEM_JAVA_CACERTS = [
    Path("/etc/ssl/certs/java/cacerts"),
    Path("/etc/pki/java/cacerts"),
    Path("/usr/lib/jvm/default-java/lib/security/cacerts"),
]
SYSTEM_CA_BUNDLES = [
    Path("/etc/ssl/certs/ca-certificates.crt"),  # Debian/Ubuntu
    Path("/etc/pki/tls/certs/ca-bundle.crt"),  # RHEL/CentOS
    Path("/etc/ssl/ca-bundle.pem"),  # OpenSUSE
    Path("/etc/ssl/cert.pem"),  # macOS, Alpine
]

# Environment variables for SSL CA bundle configuration (all should point to same CA bundle)
SSL_CA_ENV_VARS = ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"]


def _resolve_rlocation(rlocation_path: str) -> Path | None:
    """Resolve an rlocation path via Bazel runfiles, or None outside Bazel.

    The lazy import avoids crashing when proxy_setup is loaded by the
    bazel_wrapper subprocess (which runs outside Bazel runfiles).
    """
    try:
        from util.bazel.runfiles import get_required_path  # noqa: PLC0415

        return get_required_path(rlocation_path)
    except RuntimeError:
        return None


def _find_keytool() -> str:
    """Find keytool binary: Bazel rlocation, then JAVA_HOME, then PATH."""
    if env_val := os.environ.get(_KEYTOOL_RLOCATION_ENV):
        if resolved := _resolve_rlocation(env_val):
            logger.debug("Resolved keytool via rlocation: %s", resolved)
            return str(resolved)
        logger.warning("KEYTOOL_RLOCATION=%r could not be resolved", env_val)

    if java_home := os.environ.get("JAVA_HOME"):
        keytool = Path(java_home) / "bin" / "keytool"
        if keytool.exists():
            return str(keytool)

    return "keytool"


@dataclass
class ProxySetup:
    """Result of auth proxy setup."""

    port: int
    combined_ca: Path
    status: str
    ca_status: str


def _find_system_file(candidates: list[Path], description: str) -> Path:
    """Find first existing file from candidates, raise if none found."""
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Could not find {description}")


def _get_java_cacerts_candidates() -> list[Path]:
    """Get list of Java cacerts candidates.

    Resolution order: Bazel rlocation → JAVA_HOME → system locations.
    """
    candidates = []

    # Check Bazel-provided cacerts via rlocation (hermetic JDK)
    if (env_val := os.environ.get(_JAVA_CACERTS_RLOCATION_ENV)) and (resolved := _resolve_rlocation(env_val)):
        candidates.append(resolved)

    # Check JAVA_HOME (set by setup-java on GitHub Actions or local installs)
    if java_home := os.environ.get("JAVA_HOME"):
        candidates.append(Path(java_home) / "lib" / "security" / "cacerts")

    # Then check standard system locations
    candidates.extend(SYSTEM_JAVA_CACERTS)

    return candidates


def _is_anthropic_tls_inspection_ca(cert: x509.Certificate) -> bool:
    """Check if a certificate is an Anthropic TLS Inspection CA.

    The real Anthropic CA has:
    - Subject O=Anthropic
    - Subject CN contains "TLS Inspection CA"
    """
    org = _get_cert_attr(cert.subject, x509.oid.NameOID.ORGANIZATION_NAME)
    cn = _get_cert_attr(cert.subject, x509.oid.NameOID.COMMON_NAME)
    return org == ANTHROPIC_CA_ORG and ANTHROPIC_CA_CN_SUBSTRING in cn


@tracer.start_as_current_span("proxy_extract_ca")
def _extract_proxy_ca(paths: SessionPaths) -> None:
    """Load the TLS inspection CA certificate from the filesystem.

    Claude Code web containers have the Anthropic CA pre-installed.
    The path can be overridden via the ANTHROPIC_CA_PATH env var.

    Raises:
        CaExtractionError: If CA could not be loaded from filesystem.
    """
    ca_path = os.environ.get("ANTHROPIC_CA_PATH")
    ca_file = Path(ca_path) if ca_path else ANTHROPIC_CA_PREINSTALLED

    if not ca_file.exists():
        raise CaExtractionError(f"Anthropic CA not found at {ca_file}")

    ca_pem = ca_file.read_text()
    cert = x509.load_pem_x509_certificate(ca_pem.encode())
    if not _is_anthropic_tls_inspection_ca(cert):
        raise CaExtractionError(f"CA at {ca_file} is not an Anthropic TLS Inspection CA")

    logger.info("Loaded Anthropic CA from filesystem: %s", ca_file)
    paths.auth_proxy_ca_file.write_text(ca_pem)


def _get_cert_attr(name: x509.Name, oid: x509.ObjectIdentifier) -> str:
    """Get a certificate attribute by OID, or empty string if not present."""
    try:
        value = name.get_attributes_for_oid(oid)[0].value
        return value if isinstance(value, str) else value.decode()
    except (IndexError, TypeError):
        return ""


async def _create_java_truststore(paths: SessionPaths) -> None:
    """Create a Java truststore with the system CAs plus the proxy CA.

    Uses keytool (from JDK) to import the CA certificate into a copy of
    the system truststore.

    TODO: Switch back to pyjks when twofish supports Python 3.13.
    pyjks was removed because twofish (C extension dep) fails to build on 3.13.

    Raises:
        TruststoreError: If truststore could not be created.
    """
    ca_file = paths.auth_proxy_ca_file
    truststore = paths.auth_proxy_truststore

    if not ca_file.exists():
        raise TruststoreError("No CA file to add to truststore")

    try:
        system_cacerts = _find_system_file(_get_java_cacerts_candidates(), "system Java cacerts")
    except FileNotFoundError as e:
        raise TruststoreError(str(e)) from e

    logger.info("Creating custom Java truststore from %s", system_cacerts)

    try:
        # Copy system truststore to our location
        shutil.copy2(system_cacerts, truststore)
        # Make writable (system cacerts may be read-only)
        truststore.chmod(0o644)

        # Import the proxy CA using keytool
        keytool = _find_keytool()
        process = await asyncio.create_subprocess_exec(
            keytool,
            "-importcert",
            "-trustcacerts",
            "-alias",
            "anthropic-tls-inspection",
            "-file",
            str(ca_file),
            "-keystore",
            str(truststore),
            "-storepass",
            TRUSTSTORE_PASSWORD,
            "-noprompt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise TruststoreError(f"keytool failed: {stderr.decode()}")

        logger.info("Created custom Java truststore at %s", truststore)

    except OSError as e:
        raise TruststoreError(f"Failed to create truststore: {e}") from e


def _write_creds_file(creds_file: Path, https_proxy: str) -> None:
    """Write the upstream proxy URL to the credentials file.

    The proxy reads this file on each connection for hot-reload.
    """
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(https_proxy)
    logger.debug("Wrote proxy credentials to %s", creds_file)


async def _wait_for_proxy_port(port: int) -> None:
    """Wait for the proxy port to become available.

    Raises:
        ProxyServiceError: If proxy port is not listening within timeout.
    """
    try:
        await async_wait_for_port("127.0.0.1", port, timeout_secs=5.0)
    except TimeoutError as e:
        raise ProxyServiceError(f"Auth proxy port {port} not listening after 5s") from e


@tracer.start_as_current_span("proxy_create_bundle")
def _create_combined_ca_bundle(paths: SessionPaths) -> None:
    """Create a combined CA bundle with system CAs plus the proxy CA.

    This is needed for tools like uv that use SSL_CERT_FILE.

    Raises:
        CaBundleError: If bundle could not be created.
    """
    combined_ca = paths.auth_proxy_combined_ca
    ca_file_path = paths.auth_proxy_ca_file

    # Prefer extracted CA (written by _extract_proxy_ca) — it's always the validated,
    # correct CA for this session. In tests, ANTHROPIC_CA_PATH overrides the CA;
    # using the pre-installed file would bypass that override.
    ca_file = ca_file_path if ca_file_path.exists() else ANTHROPIC_CA_PREINSTALLED
    if not ca_file.exists():
        raise CaBundleError("No CA file to add to bundle")

    logger.info("Using CA from %s", ca_file)

    try:
        system_ca_bundle = _find_system_file(SYSTEM_CA_BUNDLES, "system CA bundle")
    except FileNotFoundError as e:
        raise CaBundleError(str(e)) from e

    logger.info("Creating combined CA bundle from %s", system_ca_bundle)

    # Combine system CAs with proxy CA
    combined = system_ca_bundle.read_text() + "\n" + ca_file.read_text()
    combined_ca.write_text(combined)
    logger.info("Created combined CA bundle at %s", combined_ca)


async def setup_auth_proxy(paths: SessionPaths, settings: HookSettings, proxy: AuthForwardingProxy) -> ProxySetup:
    """Set up the auth proxy environment for TLS-inspecting proxies.

    The proxy is expected to already be running in-process (started by the
    hook daemon server at startup). This function writes credentials and
    configures the CA/truststore environment.
    """
    port = settings.auth_proxy_port
    combined_ca = paths.auth_proxy_combined_ca

    if not get_upstream_proxy_url():
        logger.info("No https_proxy set, auth proxy setup not needed")
        return ProxySetup(port=port, combined_ca=combined_ca, status="not configured", ca_status="system")

    logger.info("Setting up auth proxy for TLS-inspecting proxy...")

    # Ensure proxy dir exists
    paths.auth_proxy_dir.mkdir(parents=True, exist_ok=True)

    # Write credentials to the proxy's creds file
    https_proxy = get_upstream_proxy_url()
    if not https_proxy:
        raise ProxyServiceError("No https_proxy environment variable set")

    _write_creds_file(proxy.creds_file, https_proxy)

    # Verify proxy is listening
    with tracer.start_as_current_span("proxy_wait_socket"):
        await _wait_for_proxy_port(port)
    logger.info("Auth proxy confirmed running on port %d", port)

    # Load the TLS inspection CA from filesystem
    _extract_proxy_ca(paths)

    # Create Java truststore with the CA
    with tracer.start_as_current_span("proxy_create_truststore"):
        await _create_java_truststore(paths)

    # Create combined CA bundle (for tools like uv that use SSL_CERT_FILE)
    _create_combined_ca_bundle(paths)

    status = f"running (port {port})" if proxy._running else "configured"
    ca_status = "custom CA" if combined_ca.exists() else "system"

    logger.info("Auth proxy setup complete")
    return ProxySetup(port=port, combined_ca=combined_ca, status=status, ca_status=ca_status)


def is_configured(paths: SessionPaths) -> bool:
    """Check if auth proxy is configured."""
    return paths.auth_proxy_truststore.exists()
