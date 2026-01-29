"""Auth proxy setup for Claude Code web's TLS-inspecting proxy.

Handles:
- Loading the Anthropic TLS inspection CA certificate from the filesystem
- Creating a Java truststore with the CA for Bazel
- Starting the local auth proxy
- Writing bazelrc configuration
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from mako.template import Template

from net_util.net import async_wait_for_port, is_port_in_use
from tools.claude_hooks.errors import CaBundleError, CaExtractionError, ProxyServiceError, TruststoreError
from tools.claude_hooks.proxy_vars import get_upstream_proxy_url
from tools.claude_hooks.settings import CONFIG_FILES, HookSettings
from tools.claude_hooks.supervisor.client import ProcessState, SupervisorClient

logger = logging.getLogger(__name__)

# Auth proxy supervisor service name
AUTH_PROXY_SERVICE = "auth-proxy"

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


@dataclass
class ProxySetup:
    """Result of auth proxy setup.

    Status and guidance are snapshotted at setup time rather than
    querying supervisor on each access.
    """

    port: int
    combined_ca: Path
    status: str
    ca_status: str
    guidance: str


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


def _extract_proxy_ca(settings: HookSettings) -> None:
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
    settings.get_auth_proxy_ca_file().write_text(ca_pem)


def _get_cert_attr(name: x509.Name, oid: x509.ObjectIdentifier) -> str:
    """Get a certificate attribute by OID, or empty string if not present."""
    try:
        value = name.get_attributes_for_oid(oid)[0].value
        return value if isinstance(value, str) else value.decode()
    except (IndexError, TypeError):
        return ""


async def _create_java_truststore(settings: HookSettings) -> None:
    """Create a Java truststore with the system CAs plus the proxy CA.

    Uses keytool (from JDK) to import the CA certificate into a copy of
    the system truststore.

    TODO: Switch back to pyjks when twofish supports Python 3.13.
    pyjks was removed because twofish (C extension dep) fails to build on 3.13.

    Raises:
        TruststoreError: If truststore could not be created.
    """
    ca_file = settings.get_auth_proxy_ca_file()
    truststore = settings.get_auth_proxy_truststore()

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
        process = await asyncio.create_subprocess_exec(
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise TruststoreError(f"keytool failed: {stderr.decode()}")

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
    """Build command to run auth proxy.

    Uses sys.executable -m to run the module. This works in both:
    - Bazel mode: PYTHONPATH is set and forwarded via _get_proxy_service_env()
    - Wheel mode: the package is installed, so the module is importable
    """
    proxy_port = settings.get_auth_proxy_port()
    creds_file = settings.get_auth_proxy_creds_file()
    auth_proxy_cmd = f"{sys.executable} -m tools.claude_hooks.auth_proxy.main"
    return f"{auth_proxy_cmd} --listen-port {proxy_port} --creds-file {creds_file}"


def _write_creds_file(settings: HookSettings, https_proxy: str) -> None:
    """Write the upstream proxy URL to the credentials file.

    The proxy reads this file on each connection for hot-reload.
    """
    creds_file = settings.get_auth_proxy_creds_file()
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(https_proxy)
    logger.debug("Wrote proxy credentials to %s", creds_file)


async def _wait_for_proxy_running(
    settings: HookSettings, supervisor: SupervisorClient, timeout_seconds: float = 5.0
) -> None:
    """Wait for proxy port to be listening AND supervisor to report RUNNING.

    Raises:
        ProxyServiceError: If proxy does not become ready within timeout.
    """
    proxy_port = settings.get_auth_proxy_port()
    try:
        async with asyncio.timeout(timeout_seconds):
            await async_wait_for_port("127.0.0.1", proxy_port, timeout_secs=timeout_seconds)
            await supervisor.wait_for_service_running(AUTH_PROXY_SERVICE)
    except TimeoutError:
        port_ready = is_port_in_use(proxy_port)
        state = await supervisor.get_service_state(AUTH_PROXY_SERVICE)
        raise ProxyServiceError(
            f"Auth proxy did not become ready within {timeout_seconds}s "
            f"(port_listening={port_ready}, supervisor_state={state})"
        )


async def ensure_proxy_running(settings: HookSettings, supervisor: SupervisorClient) -> None:
    """Ensure proxy is running with current credentials.

    Writes the current https_proxy URL to the credentials file. The proxy
    reads this file on each connection, so credential changes take effect
    immediately without restart.

    Raises:
        ProxyServiceError: If https_proxy not set or proxy fails to start.
    """
    proxy_dir = settings.get_auth_proxy_dir()
    proxy_dir.mkdir(parents=True, exist_ok=True)

    https_proxy = get_upstream_proxy_url()
    if not https_proxy:
        raise ProxyServiceError("No https_proxy environment variable set")

    # Write current proxy URL (proxy reads on each connection)
    _write_creds_file(settings, https_proxy)

    # If proxy is already running, we're done (it will pick up new creds)
    if await supervisor.is_service_running(AUTH_PROXY_SERVICE):
        return

    # Service exists but not running (FATAL/STOPPED/EXITED) - restart it
    command = _build_auth_proxy_command(settings)
    proxy_port = settings.get_auth_proxy_port()
    if await supervisor.service_exists(AUTH_PROXY_SERVICE):
        logger.info("Restarting proxy service on port %d", proxy_port)
        await supervisor.update_service(
            name=AUTH_PROXY_SERVICE, command=command, directory=proxy_dir, environment=_get_proxy_service_env()
        )
    else:
        # Start proxy service for the first time
        logger.info("Starting auth proxy on port %d via supervisor", proxy_port)
        await supervisor.add_service(
            name=AUTH_PROXY_SERVICE, command=command, directory=proxy_dir, environment=_get_proxy_service_env()
        )

    await _wait_for_proxy_running(settings, supervisor)
    logger.info("Auth proxy running successfully")


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
    """Write Bazel config to separate file for auth proxy integration."""
    truststore = settings.get_auth_proxy_truststore()
    combined_ca = settings.get_auth_proxy_combined_ca()
    proxy_rc = settings.get_auth_proxy_rc()
    proxy_port = settings.get_auth_proxy_port()

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
    combined_ca = settings.get_auth_proxy_combined_ca()
    ca_file_path = settings.get_auth_proxy_ca_file()

    # Prefer pre-installed Anthropic CA, fall back to extracted one
    ca_file = ANTHROPIC_CA_PREINSTALLED if ANTHROPIC_CA_PREINSTALLED.exists() else ca_file_path
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


async def _snapshot_proxy_status(settings: HookSettings, supervisor: SupervisorClient, port: int) -> str:
    """Snapshot the current proxy status."""
    if not settings.get_auth_proxy_truststore().exists():
        return "not configured"
    if await supervisor.is_service_running(AUTH_PROXY_SERVICE):
        return f"running (port {port})"
    return "configured (not running)"


async def _snapshot_proxy_guidance(supervisor: SupervisorClient, port: int, combined_ca: Path) -> str:
    """Snapshot the proxy configuration guidance."""
    if not get_upstream_proxy_url():
        return ""

    try:
        info = await supervisor.get_process_info(AUTH_PROXY_SERVICE)
        service_status = info.statename
    except Exception:
        service_status = ProcessState.UNKNOWN
    ca_info = f"Custom CA bundle: {combined_ca}" if combined_ca.exists() else "Using system CA bundle"

    return textwrap.dedent(
        f"""\
        Auth Proxy Configuration
        ========================
        Auth proxy port: {port}
        Service status: {service_status}
        {ca_info}

        The proxy handles:
        - Authentication forwarding to upstream proxy
        - TLS inspection CA certificate management
        - Java truststore configuration for Bazel

        Environment variables are automatically configured in CLAUDE_ENV_FILE.
        """
    )


async def setup_auth_proxy(settings: HookSettings, supervisor: SupervisorClient) -> ProxySetup:
    """Set up the complete auth proxy environment for TLS-inspecting proxies.

    This is needed when running behind Anthropic's TLS-inspecting proxy
    (Claude Code web). Steps:
    1. Start auth proxy (handles auth to upstream)
    2. Extract the TLS inspection CA (via auth proxy)
    3. Create Java truststore with the CA
    4. Create combined CA bundle for SSL tools
    5. Write bazelrc configuration to use the proxy

    Note: Proxy env for Bazel rules is handled by the module extension in
    tools/claude_hooks/auth_proxy/proxy_config_defs.bzl which reads AUTH_PROXY_PORT env var.

    Returns:
        ProxySetup with port, CA path, and snapshotted status/guidance
    """
    port = settings.get_auth_proxy_port()
    combined_ca = settings.get_auth_proxy_combined_ca()

    if not get_upstream_proxy_url():
        logger.info("No https_proxy set, auth proxy setup not needed")
        return ProxySetup(port=port, combined_ca=combined_ca, status="not configured", ca_status="system", guidance="")

    logger.info("Setting up auth proxy for TLS-inspecting proxy...")

    # Ensure proxy dir exists
    settings.get_auth_proxy_dir().mkdir(parents=True, exist_ok=True)

    # Step 1: Start auth proxy first (needed for CA extraction)
    await ensure_proxy_running(settings, supervisor)

    # Step 2: Load the TLS inspection CA from filesystem
    _extract_proxy_ca(settings)

    # Step 3: Create Java truststore with the CA
    await _create_java_truststore(settings)

    # Step 4: Create combined CA bundle (for tools like uv that use SSL_CERT_FILE)
    _create_combined_ca_bundle(settings)

    # Step 5: Write bazelrc configuration
    _write_bazel_config(settings)

    # Snapshot status and guidance at setup completion
    status = await _snapshot_proxy_status(settings, supervisor, port)
    ca_status = "custom CA" if combined_ca.exists() else "system"
    guidance = await _snapshot_proxy_guidance(supervisor, port, combined_ca)

    logger.info("Auth proxy setup complete")
    return ProxySetup(port=port, combined_ca=combined_ca, status=status, ca_status=ca_status, guidance=guidance)


def is_configured(settings: HookSettings) -> bool:
    """Check if auth proxy is configured."""
    return settings.get_auth_proxy_truststore().exists()
