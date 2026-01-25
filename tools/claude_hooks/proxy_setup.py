"""Bazel proxy setup for Claude Code web's TLS-inspecting proxy.

Handles:
- Extracting the Anthropic TLS inspection CA certificate from the proxy
- Creating a Java truststore with the CA for Bazel
- Starting the local bazel proxy wrapper
- Writing bazelrc configuration
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import ssl
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from mako.template import Template

from tools.claude_hooks.errors import CaBundleError, CaExtractionError, ProxyServiceError, TruststoreError
from tools.claude_hooks.resources import CONFIG_FILES
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import SupervisorClient

logger = logging.getLogger(__name__)

# Bazel proxy configuration
BAZEL_PROXY_SERVICE = "bazel-proxy"  # supervisor service name

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
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
]

# Maximum size for HTTP CONNECT response (protects against malformed proxy responses)
MAX_CONNECT_RESPONSE_SIZE = 64 * 1024  # 64 KB


@dataclass
class ProxySetup:
    """Result of bazel proxy setup."""

    port: int
    combined_ca: Path
    supervisor: SupervisorClient
    settings: HookSettings

    @property
    def status(self) -> str:
        """Get human-readable proxy status."""
        if not self.settings.get_bazel_truststore().exists():
            return "not configured"
        if self.supervisor.is_service_running(BAZEL_PROXY_SERVICE, wait_for_start=False):
            return f"running (port {self.port})"
        return "configured (not running)"

    @property
    def ca_status(self) -> str:
        """Get CA bundle status."""
        return "custom CA" if self.combined_ca.exists() else "system"

    @property
    def guidance(self) -> str:
        """Get proxy configuration guidance."""
        if not _get_https_proxy():
            return ""

        info = self.supervisor.get_process_info(BAZEL_PROXY_SERVICE)
        service_status = info.statename if info else "UNKNOWN"
        ca_info = f"Custom CA bundle: {self.combined_ca}" if self.combined_ca.exists() else "Using system CA bundle"

        return textwrap.dedent(
            f"""\
            Bazel Proxy Configuration
            =========================
            Local proxy port: {self.port}
            Service status: {service_status}
            {ca_info}

            The proxy handles:
            - Authentication forwarding to upstream proxy
            - TLS inspection CA certificate management
            - Java truststore configuration for Bazel

            Environment variables are automatically configured in CLAUDE_ENV_FILE.
            """
        )


def _get_https_proxy() -> str | None:
    """Get https_proxy from environment (case-insensitive)."""
    return os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")


def _find_system_file(candidates: list[Path], description: str) -> Path:
    """Find first existing file from candidates, raise if none found."""
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Could not find {description}")


def _get_java_cacerts_candidates() -> list[Path]:
    """Get list of Java cacerts candidates, including JAVA_HOME if set.

    JAVA_HOME is checked first because on CI (GitHub Actions with setup-java),
    Java is installed to non-standard locations like /opt/hostedtoolcache/...
    """
    candidates = []

    # Check JAVA_HOME first (set by setup-java on GitHub Actions)
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


def _try_load_ca_from_filesystem(settings: HookSettings) -> bool:
    """Try to load CA from pre-installed filesystem location.

    Claude Code web containers have the CA pre-installed at:
    /usr/local/share/ca-certificates/swp-ca-production.crt

    Returns True if CA was loaded successfully, False otherwise.
    """
    ca_path = os.environ.get("ANTHROPIC_CA_PATH")
    ca_file = Path(ca_path) if ca_path else ANTHROPIC_CA_PREINSTALLED

    if not ca_file.exists():
        return False

    try:
        ca_pem = ca_file.read_text()
        # Verify it's a valid PEM certificate with expected subject
        cert = x509.load_pem_x509_certificate(ca_pem.encode())
        if not _is_anthropic_tls_inspection_ca(cert):
            logger.warning("CA at %s is not an Anthropic TLS Inspection CA", ca_file)
            return False

        logger.info("Loaded Anthropic CA from filesystem: %s", ca_file)
        settings.get_bazel_ca_file().write_text(ca_pem)
        return True
    except (OSError, ValueError) as e:
        logger.warning("Failed to load CA from %s: %s", ca_file, e)
        return False


def _extract_proxy_ca(settings: HookSettings) -> None:
    """Extract the TLS inspection CA certificate.

    First tries to load from the pre-installed filesystem location (faster, no network).
    Falls back to extracting from the TLS chain via proxy connection.

    Uses Python 3.13+ ssl.SSLSocket.get_unverified_chain() for cert chain access.

    Raises:
        CaExtractionError: If CA could not be extracted.
    """
    # Prefer filesystem (pre-installed on Claude Code web containers)
    if _try_load_ca_from_filesystem(settings):
        return

    # Fall back to extracting from TLS chain
    proxy_port = settings.get_bazel_proxy_port()
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
        # Match on subject: O=Anthropic AND CN contains "TLS Inspection CA"
        for i, der_bytes in enumerate(cert_chain_der):
            cert = x509.load_der_x509_certificate(der_bytes)
            if _is_anthropic_tls_inspection_ca(cert):
                cn = _get_cert_attr(cert.subject, x509.oid.NameOID.COMMON_NAME)
                logger.info("Found Anthropic TLS inspection CA at position %d: %s", i, cn)
                pem_cert = cert.public_bytes(Encoding.PEM).decode()
                settings.get_bazel_ca_file().write_text(pem_cert)
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


def _create_java_truststore(settings: HookSettings) -> None:
    """Create a Java truststore with the system CAs plus the proxy CA.

    Uses keytool (from JDK) to import the CA certificate into a copy of
    the system truststore.

    TODO: Switch back to pyjks when twofish supports Python 3.13.
    pyjks was removed because twofish (C extension dep) fails to build on 3.13.

    Raises:
        TruststoreError: If truststore could not be created.
    """
    ca_file = settings.get_bazel_ca_file()
    truststore = settings.get_bazel_truststore()

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
        result = subprocess.run(
            [
                "keytool",
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
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise TruststoreError(f"keytool failed: {result.stderr}")

        logger.info("Created custom Java truststore at %s", truststore)

    except OSError as e:
        raise TruststoreError(f"Failed to create truststore: {e}") from e


def _get_proxy_service_env() -> dict[str, str]:
    """Get environment variables to pass to the proxy service."""
    env: dict[str, str] = {}
    if pythonpath := os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = pythonpath
    return env


def _build_auth_proxy_command(settings: HookSettings) -> str:
    """Build command to run custom auth-forwarding proxy."""
    proxy_port = settings.get_bazel_proxy_port()
    creds_file = settings.get_bazel_creds_file()
    return f"{settings.auth_proxy_cmd} --listen-port {proxy_port} --creds-file {creds_file}"


def _write_creds_file(settings: HookSettings, https_proxy: str) -> None:
    """Write the upstream proxy URL to the credentials file.

    The proxy reads this file on each connection for hot-reload.
    """
    creds_file = settings.get_bazel_creds_file()
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(https_proxy)
    logger.debug("Wrote proxy credentials to %s", creds_file)


def _wait_for_proxy(settings: HookSettings, timeout_seconds: float = 5.0) -> bool:
    """Wait for proxy to start listening, return True if successful."""
    proxy_port = settings.get_bazel_proxy_port()
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


def ensure_proxy_running(settings: HookSettings, supervisor: SupervisorClient) -> None:
    """Ensure proxy is running with current credentials.

    Writes the current https_proxy URL to the credentials file. The proxy
    reads this file on each connection, so credential changes take effect
    immediately without restart.

    Raises:
        ProxyServiceError: If https_proxy not set or proxy fails to start.
    """
    proxy_dir = settings.get_bazel_proxy_dir()
    proxy_dir.mkdir(parents=True, exist_ok=True)

    https_proxy = _get_https_proxy()
    if not https_proxy:
        raise ProxyServiceError("No https_proxy environment variable set")

    # Write current proxy URL (proxy reads on each connection)
    _write_creds_file(settings, https_proxy)

    # If proxy is already running, we're done (it will pick up new creds)
    if supervisor.is_service_running(BAZEL_PROXY_SERVICE):
        return

    # Service exists but not running (FATAL/STOPPED/EXITED) - restart it
    command = _build_auth_proxy_command(settings)
    proxy_port = settings.get_bazel_proxy_port()
    current_command = supervisor.get_service_command(BAZEL_PROXY_SERVICE)
    if current_command is not None:
        logger.info("Restarting proxy service on port %d", proxy_port)
        supervisor.update_service(
            name=BAZEL_PROXY_SERVICE, command=command, directory=proxy_dir, environment=_get_proxy_service_env()
        )
        if not _wait_for_proxy(settings):
            raise ProxyServiceError("Bazel proxy did not start listening after restart")
        logger.info("Bazel proxy restarted successfully")
        return

    # Start proxy service
    logger.info("Starting auth-forwarding proxy on port %d via supervisor", proxy_port)
    supervisor.add_service(
        name=BAZEL_PROXY_SERVICE, command=command, directory=proxy_dir, environment=_get_proxy_service_env()
    )
    if not _wait_for_proxy(settings):
        raise ProxyServiceError("Bazel proxy did not start listening in time")
    logger.info("Bazel proxy started successfully")


def _get_local_registry_path() -> Path | None:
    """Get local registry path if it exists in the project directory."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return None
    local_registry = Path(project_dir) / "tools" / "local_registry"
    if local_registry.exists() and (local_registry / "bazel_registry.json").exists():
        return local_registry
    return None


def _write_bazel_config(settings: HookSettings) -> None:
    """Write Bazel proxy config to separate file.

    The caller should set BAZEL_SYSTEM_BAZELRC_PATH env var to include this file.
    """
    truststore = settings.get_bazel_truststore()
    combined_ca = settings.get_bazel_combined_ca()
    proxy_rc = settings.get_bazel_proxy_rc()
    proxy_port = settings.get_bazel_proxy_port()

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


def _create_combined_ca_bundle(settings: HookSettings) -> None:
    """Create a combined CA bundle with system CAs plus the proxy CA.

    This is needed for tools like uv that use SSL_CERT_FILE.

    Raises:
        CaBundleError: If bundle could not be created.
    """
    combined_ca = settings.get_bazel_combined_ca()
    bazel_ca_file = settings.get_bazel_ca_file()

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


def setup_bazel_proxy(settings: HookSettings, supervisor: SupervisorClient) -> ProxySetup:
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

    Returns:
        ProxySetup with port, CA path, and status information
    """
    port = settings.get_bazel_proxy_port()
    combined_ca = settings.get_bazel_combined_ca()

    if not _get_https_proxy():
        logger.info("No https_proxy set, Bazel proxy setup not needed")
        return ProxySetup(port=port, combined_ca=combined_ca, supervisor=supervisor, settings=settings)

    logger.info("Setting up Bazel proxy for TLS-inspecting proxy...")

    # Ensure proxy dir exists
    settings.get_bazel_proxy_dir().mkdir(parents=True, exist_ok=True)

    # Step 1: Start local proxy first (needed for CA extraction)
    ensure_proxy_running(settings, supervisor)

    # Step 2: Extract the TLS inspection CA (via local proxy)
    _extract_proxy_ca(settings)

    # Step 3: Create Java truststore with the CA
    _create_java_truststore(settings)

    # Step 4: Create combined CA bundle (for tools like uv that use SSL_CERT_FILE)
    _create_combined_ca_bundle(settings)

    # Step 5: Write bazelrc configuration
    _write_bazel_config(settings)

    logger.info("Bazel proxy setup complete")
    return ProxySetup(port=port, combined_ca=combined_ca, supervisor=supervisor, settings=settings)


def is_configured(settings: HookSettings) -> bool:
    """Check if Bazel proxy is configured."""
    return settings.get_bazel_truststore().exists()
