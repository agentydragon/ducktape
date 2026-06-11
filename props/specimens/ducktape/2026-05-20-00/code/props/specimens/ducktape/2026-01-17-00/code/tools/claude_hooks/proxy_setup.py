"""Bazel proxy setup for Claude Code web's TLS-inspecting proxy.

Handles:
- Extracting the Anthropic TLS inspection CA certificate from the proxy
- Creating a Java truststore with the CA for Bazel
- Starting the local bazel proxy wrapper
- Writing bazelrc configuration

Configuration via environment variables (for testing):
- CLAUDE_HOOKS_BAZEL_PROXY_DIR: Override proxy directory
- CLAUDE_HOOKS_BAZEL_PROXY_PORT: Override proxy port
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import ssl
import time
from pathlib import Path

import jks
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from mako.template import Template

from claude_hooks import supervisor_setup
from claude_hooks.errors import CaBundleError, CaExtractionError, ProxyServiceError, TruststoreError
from claude_hooks.proxy_credentials import build_upstream_uri, parse_proxy_url
from claude_hooks.resources import CONFIG_FILES

logger = logging.getLogger(__name__)


def _get_bazel_proxy_port() -> int:
    """Get bazel proxy port, allowing override via env var."""
    if env_port := os.environ.get("CLAUDE_HOOKS_BAZEL_PROXY_PORT"):
        return int(env_port)
    return 18081


def _get_bazel_proxy_dir() -> Path:
    """Get bazel proxy directory, allowing override via env var."""
    if env_dir := os.environ.get("CLAUDE_HOOKS_BAZEL_PROXY_DIR"):
        return Path(env_dir)
    return Path.home() / ".cache" / "bazel-proxy"


# Bazel proxy configuration
BAZEL_PROXY_SERVICE = "bazel-proxy"  # supervisor service name


def _get_bazel_creds_file() -> Path:
    return _get_bazel_proxy_dir() / "upstream_proxy"


def _get_bazel_ca_file() -> Path:
    return _get_bazel_proxy_dir() / "anthropic_ca.pem"


def _get_bazel_combined_ca() -> Path:
    return _get_bazel_proxy_dir() / "combined_ca.pem"


def _get_bazel_truststore() -> Path:
    return _get_bazel_proxy_dir() / "cacerts.jks"


def _get_bazel_proxy_rc() -> Path:
    return _get_bazel_proxy_dir() / "bazelrc"


# Pre-installed Anthropic CA on Claude Code web containers
ANTHROPIC_CA_PREINSTALLED = Path("/usr/local/share/ca-certificates/swp-ca-production.crt")

# Java truststore password (standard default)
TRUSTSTORE_PASSWORD = "changeit"

# System file locations with fallbacks
SYSTEM_JAVA_CACERTS = [
    Path("/etc/ssl/certs/java/cacerts"),
    Path("/etc/pki/java/cacerts"),
    Path("/usr/lib/jvm/default-java/lib/security/cacerts"),
]
SYSTEM_CA_BUNDLES = [
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
]

# Maximum size for HTTP CONNECT response (protects against malformed proxy responses)
MAX_CONNECT_RESPONSE_SIZE = 64 * 1024  # 64 KB


def _get_https_proxy() -> str | None:
    """Get https_proxy from environment (case-insensitive)."""
    return os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")


def _find_system_file(candidates: list[Path], description: str) -> Path:
    """Find first existing file from candidates, raise if none found."""
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Could not find {description}")


def _extract_proxy_ca() -> None:
    """Extract the TLS inspection CA certificate from the proxy.

    Uses our local proxy (localhost:18081) which handles auth to upstream.
    Connects through HTTP CONNECT tunnel, then performs TLS handshake to get certs.
    Uses Python 3.13+ ssl.SSLSocket.get_unverified_chain() for cert chain access.

    Raises:
        CaExtractionError: If CA could not be extracted.
    """
    proxy_port = _get_bazel_proxy_port()
    logger.info("Extracting TLS inspection CA via local proxy localhost:%d", proxy_port)

    sock = None
    ssl_sock = None
    try:
        # Connect to local proxy
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=30)

        # Send HTTP CONNECT request through proxy
        connect_request = b"CONNECT bcr.bazel.build:443 HTTP/1.1\r\nHost: bcr.bazel.build:443\r\n\r\n"
        sock.sendall(connect_request)

        # Read CONNECT response
        response = b""
        while b"\r\n\r\n" not in response:
            if len(response) > MAX_CONNECT_RESPONSE_SIZE:
                raise CaExtractionError("Proxy CONNECT response too large")
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

        status_line = response.split(b"\r\n", 1)[0]
        if not status_line.startswith(b"HTTP/") or b" 200 " not in status_line:
            raise CaExtractionError(f"CONNECT failed: {status_line.decode(errors='replace')}")

        # Use stdlib ssl with verification disabled (we want to inspect the proxy's cert)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ssl_sock = ctx.wrap_socket(sock, server_hostname="bcr.bazel.build")

        # Get certificate chain (Python 3.13+ API)
        cert_chain_der = ssl_sock.get_unverified_chain()

        if not cert_chain_der:
            raise CaExtractionError("No certificates in chain")

        # Find the Anthropic TLS inspection CA in the chain
        for i, der_bytes in enumerate(cert_chain_der):
            cert = x509.load_der_x509_certificate(der_bytes)
            subject_cn = _get_cert_attr(cert.subject, x509.oid.NameOID.COMMON_NAME)
            issuer_cn = _get_cert_attr(cert.issuer, x509.oid.NameOID.COMMON_NAME)
            org = _get_cert_attr(cert.subject, x509.oid.NameOID.ORGANIZATION_NAME)

            if "Anthropic" in subject_cn or "Anthropic" in issuer_cn or "Anthropic" in org:
                logger.info("Found Anthropic TLS inspection CA at position %d: %s", i, subject_cn)
                pem_cert = cert.public_bytes(Encoding.PEM).decode()
                _get_bazel_ca_file().write_text(pem_cert)
                return

        raise CaExtractionError("Could not find Anthropic TLS inspection CA in chain")

    except (OSError, ssl.SSLError) as e:
        raise CaExtractionError(f"Failed to extract proxy CA: {e}") from e
    finally:
        if ssl_sock is not None:
            with contextlib.suppress(ssl.SSLError):
                ssl_sock.close()
        elif sock is not None:
            sock.close()


def _get_cert_attr(name: x509.Name, oid: x509.ObjectIdentifier) -> str:
    """Get a certificate attribute by OID, or empty string if not present."""
    try:
        value = name.get_attributes_for_oid(oid)[0].value
        return value if isinstance(value, str) else value.decode()
    except (IndexError, TypeError):
        return ""


def _create_java_truststore() -> None:
    """Create a Java truststore with the system CAs plus the proxy CA.

    Uses pyjks to manipulate Java keystores without requiring keytool.

    Raises:
        TruststoreError: If truststore could not be created.
    """
    ca_file = _get_bazel_ca_file()
    truststore = _get_bazel_truststore()

    if not ca_file.exists():
        raise TruststoreError("No CA file to add to truststore")

    try:
        system_cacerts = _find_system_file(SYSTEM_JAVA_CACERTS, "system Java cacerts")
    except FileNotFoundError as e:
        raise TruststoreError(str(e)) from e

    logger.info("Creating custom Java truststore from %s", system_cacerts)

    try:
        # Load system truststore
        keystore = jks.KeyStore.load(str(system_cacerts), TRUSTSTORE_PASSWORD)

        # Load and parse the proxy CA
        ca_pem = ca_file.read_text()
        ca_cert = x509.load_pem_x509_certificate(ca_pem.encode())
        ca_der = ca_cert.public_bytes(Encoding.DER)

        # Create trusted cert entry and add to keystore
        entry = jks.TrustedCertEntry.new("X.509", ca_der)
        keystore.entries["anthropic-tls-inspection"] = entry

        # Save the modified truststore
        keystore.save(str(truststore), TRUSTSTORE_PASSWORD)

        logger.info("Created custom Java truststore at %s", truststore)

    except (jks.KeystoreException, OSError, ValueError) as e:
        raise TruststoreError(f"Failed to create truststore: {e}") from e


def _build_pproxy_command(https_proxy: str) -> str:
    """Build pproxy command with credentials embedded in upstream URI."""
    proxy = parse_proxy_url(https_proxy)
    upstream_uri = build_upstream_uri(proxy)
    proxy_port = _get_bazel_proxy_port()

    # pproxy CLI: -l = listen, -r = remote upstream
    return f"pproxy -l http://127.0.0.1:{proxy_port}/ -r {upstream_uri}/"


def _wait_for_proxy(timeout_seconds: float = 5.0) -> bool:
    """Wait for proxy to start listening, return True if successful."""
    proxy_port = _get_bazel_proxy_port()
    for _ in range(int(timeout_seconds / 0.5)):
        time.sleep(0.5)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn_result = sock.connect_ex(("127.0.0.1", proxy_port))
            sock.close()
            if conn_result == 0:
                return True
        except OSError:
            pass  # Expected: connection refused during startup
    return False


def ensure_proxy_running() -> bool:
    """Ensure proxy is running with current credentials, starting or updating as needed.

    Handles all proxy service management:
    - Starts supervisor if not running
    - Starts proxy service if not running
    - Updates credentials and restarts if they've changed

    Returns True if credentials were refreshed (proxy restarted).

    Raises:
        ProxyServiceError: If https_proxy not set or proxy fails to start.
    """
    https_proxy = _get_https_proxy()
    if not https_proxy:
        raise ProxyServiceError("No https_proxy environment variable set")

    proxy_dir = _get_bazel_proxy_dir()
    creds_file = _get_bazel_creds_file()

    proxy_dir.mkdir(parents=True, exist_ok=True)

    # Ensure supervisor is running
    supervisor_setup.start()

    command = _build_pproxy_command(https_proxy)

    # Check if service is running
    if supervisor_setup.is_service_running(BAZEL_PROXY_SERVICE):
        # Check if credentials changed
        if creds_file.exists():
            cached = creds_file.read_text().strip()
            if cached == https_proxy:
                return False  # Already running with correct credentials

        # Update credentials and restart
        logger.info("Updating proxy with new credentials...")
        creds_file.write_text(https_proxy)
        supervisor_setup.update_service(name=BAZEL_PROXY_SERVICE, command=command, directory=proxy_dir)
        if not _wait_for_proxy():
            raise ProxyServiceError("Proxy did not restart with new credentials")
        logger.info("Proxy credentials refreshed")
        return True

    # Start proxy service
    proxy_port = _get_bazel_proxy_port()
    logger.info("Starting pproxy on port %d via supervisor", proxy_port)
    creds_file.write_text(https_proxy)
    supervisor_setup.add_service(name=BAZEL_PROXY_SERVICE, command=command, directory=proxy_dir)
    if not _wait_for_proxy():
        raise ProxyServiceError("Bazel proxy did not start listening in time")
    logger.info("Bazel proxy started successfully")
    return False


def _get_local_registry_path() -> Path | None:
    """Get local registry path if it exists in the project directory."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return None
    local_registry = Path(project_dir) / "tools" / "local_registry"
    if local_registry.exists() and (local_registry / "bazel_registry.json").exists():
        return local_registry
    return None


def _write_bazel_config() -> None:
    """Write Bazel proxy config to separate file.

    The caller should set BAZEL_SYSTEM_BAZELRC_PATH env var to include this file.
    """
    truststore = _get_bazel_truststore()
    combined_ca = _get_bazel_combined_ca()
    proxy_rc = _get_bazel_proxy_rc()
    proxy_port = _get_bazel_proxy_port()

    if not truststore.exists():
        logger.warning("No truststore, skipping bazelrc")
        return

    local_proxy = f"http://localhost:{proxy_port}"

    # Check for local registry (contains patched ape module for native ELF support)
    local_registry = _get_local_registry_path()
    if local_registry:
        logger.info("Found local registry at %s (patched ape for native ELF)", local_registry)

    # Combined CA bundle must exist at this point (created by _create_combined_ca_bundle)
    if not combined_ca.exists():
        raise CaBundleError("Combined CA bundle not found - setup incomplete")

    # Render bazelrc from template
    template = Template(CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"])
    result: str = template.render(
        proxy_port=proxy_port,
        truststore_path=truststore,
        truststore_password=TRUSTSTORE_PASSWORD,
        local_proxy=local_proxy,
        combined_ca_path=combined_ca,
        local_registry_path=local_registry,
    )
    proxy_rc.write_text(result)
    logger.info("Wrote proxy config to %s", proxy_rc)


def _create_combined_ca_bundle() -> None:
    """Create a combined CA bundle with system CAs plus the proxy CA.

    This is needed for tools like uv that use SSL_CERT_FILE.

    Raises:
        CaBundleError: If bundle could not be created.
    """
    combined_ca = _get_bazel_combined_ca()
    bazel_ca_file = _get_bazel_ca_file()

    # Prefer pre-installed Anthropic CA, fall back to extracted one
    ca_file = ANTHROPIC_CA_PREINSTALLED if ANTHROPIC_CA_PREINSTALLED.exists() else bazel_ca_file
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


def setup_bazel_proxy() -> None:
    """Set up the complete Bazel proxy environment for TLS-inspecting proxies.

    This is needed when running behind Anthropic's TLS-inspecting proxy
    (Claude Code web). Steps:
    1. Start local proxy (handles auth to upstream)
    2. Extract the TLS inspection CA (via local proxy)
    3. Create Java truststore with the CA
    4. Create combined CA bundle for SSL tools
    5. Write bazelrc configuration to use the proxy

    Note: Proxy env for Bazel rules is handled by the module extension in
    tools/proxy_config/defs.bzl which reads BAZEL_PROXY_PORT env var.
    """
    if not _get_https_proxy():
        logger.info("No https_proxy set, Bazel proxy setup not needed")
        return

    logger.info("Setting up Bazel proxy for TLS-inspecting proxy...")

    # Step 1: Start local proxy first (needed for CA extraction)
    ensure_proxy_running()

    # Step 2: Extract the TLS inspection CA (via local proxy)
    _extract_proxy_ca()

    # Step 3: Create Java truststore with the CA
    _create_java_truststore()

    # Step 4: Create combined CA bundle (for tools like uv that use SSL_CERT_FILE)
    _create_combined_ca_bundle()

    # Step 5: Write bazelrc configuration
    _write_bazel_config()

    logger.info("Bazel proxy setup complete")


def is_configured() -> bool:
    """Check if Bazel proxy is configured."""
    return _get_bazel_truststore().exists()


def get_status() -> str:
    """Get human-readable proxy status."""
    if not _get_bazel_truststore().exists():
        return "not configured"
    if supervisor_setup.is_service_running(BAZEL_PROXY_SERVICE):
        return f"running (port {_get_bazel_proxy_port()})"
    return "configured but not running"
