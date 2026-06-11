"""Mock egress proxy for testing.

Simulates the behavior of Anthropic's TLS-inspecting egress proxy:
- Requires Basic auth on CONNECT requests
- Performs TLS interception with a mock CA matching real CA format
- Forwards traffic to real destinations (or chains through upstream proxy)

Used for e2e testing of the session_start hook and auth proxy infrastructure.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import select
import socket
import ssl
import tempfile
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS
from tools.claude_hooks.proxy_vars import get_upstream_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class EgressProxyConfig:
    """Configuration for upstream proxy."""

    host: str
    port: int
    username: str | None = None
    password: str | None = None
    ca_bundle: str | None = None  # Path to CA bundle for verifying upstream TLS

    @classmethod
    def from_env(cls) -> EgressProxyConfig | None:
        """Parse upstream proxy from environment variables.

        Looks for HTTPS_PROXY or https_proxy in format:
        http://user:pass@host:port or http://host:port

        Localhost proxies (e.g. the auth proxy at localhost:18081)
        are valid upstream targets — they forward to the real egress proxy.
        """
        proxy_url = get_upstream_proxy_url()
        if not proxy_url:
            return None

        parsed = urllib.parse.urlparse(proxy_url)
        if not parsed.hostname:
            return None

        # Get CA bundle for verifying upstream proxy's TLS
        ca_bundle = next((v for var in SSL_CA_ENV_VARS if (v := os.environ.get(var))), None)

        return cls(
            host=parsed.hostname,
            port=parsed.port or 8080,
            username=urllib.parse.unquote(parsed.username) if parsed.username else None,
            password=urllib.parse.unquote(parsed.password) if parsed.password else None,
            ca_bundle=ca_bundle,
        )


def generate_mock_ca() -> tuple[bytes, bytes]:
    """Generate a self-signed CA cert matching Anthropic's real CA format.

    The real Anthropic CA (from /usr/local/share/ca-certificates/swp-ca-production.crt) has:
    - Subject: O=Anthropic, CN=sandbox-egress-production TLS Inspection CA
    - Self-signed (issuer = subject)
    - RSA 2048-bit key
    - 10-year validity
    - KeyUsage: critical - Certificate Sign, CRL Sign
    - ExtendedKeyUsage: TLS Web Server Authentication
    - BasicConstraints: critical - CA:TRUE
    - SubjectKeyIdentifier
    - AuthorityKeyIdentifier (self-referential for self-signed)

    Returns (cert_pem, key_pem) tuple.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Match the real Anthropic CA certificate format
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Anthropic"),
            x509.NameAttribute(NameOID.COMMON_NAME, "sandbox-egress-production TLS Inspection CA"),
        ]
    )

    # SubjectKeyIdentifier is required for CA certs (used by AKI in issued certs)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())

    # AuthorityKeyIdentifier - self-referential for self-signed CA
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key())

    # KeyUsage is required for CA certs - allows signing certs and CRLs
    key_usage = x509.KeyUsage(
        key_cert_sign=True,
        crl_sign=True,
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        encipher_only=False,
        decipher_only=False,
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=3650))  # 10 years like real CA
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(key_usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(aki, critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return cert_pem, key_pem


def generate_server_cert(ca_cert_pem: bytes, ca_key_pem: bytes, hostname: str) -> tuple[bytes, bytes]:
    """Generate a server certificate signed by the CA for a specific hostname.

    Matches real Anthropic proxy server certs (inspected via TLS interception):
    - Subject CN = target hostname (truncated to 64 chars if needed)
    - Issuer = Anthropic CA
    - 24h validity (real proxy caches and rotates multiple certs per hostname)
    - KeyUsage: critical - Digital Signature, Key Encipherment
    - ExtendedKeyUsage: TLS Web Server Authentication
    - BasicConstraints: critical - CA:FALSE
    - SubjectKeyIdentifier
    - AuthorityKeyIdentifier pointing to CA's SubjectKeyIdentifier
    - SubjectAlternativeName with DNS name

    Returns (cert_pem, key_pem) tuple.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # CN has a 64-character limit; truncate if needed (SAN is authoritative anyway)
    cn_hostname = hostname[:64] if len(hostname) > 64 else hostname
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_hostname)])

    # SubjectKeyIdentifier for this server cert
    ski = x509.SubjectKeyIdentifier.from_public_key(server_key.public_key())

    # AuthorityKeyIdentifier links this cert to the CA (required for chain validation)
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key())  # type: ignore[arg-type]

    # KeyUsage for TLS server certificates
    key_usage = x509.KeyUsage(
        digital_signature=True,
        key_encipherment=True,
        key_cert_sign=False,
        crl_sign=False,
        content_commitment=False,
        data_encipherment=False,
        key_agreement=False,
        encipher_only=False,
        decipher_only=False,
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(key_usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(ski, critical=False)
        .add_extension(aki, critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return cert_pem, key_pem


@dataclass
class ConnectionStats:
    """Track connection statistics for debugging."""

    total_connections: int = 0
    successful_connections: int = 0
    failed_connections: int = 0
    bytes_forwarded: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_success(self, bytes_count: int = 0) -> None:
        with self.lock:
            self.successful_connections += 1
            self.bytes_forwarded += bytes_count

    def record_failure(self, error: str) -> None:
        with self.lock:
            self.failed_connections += 1
            self.errors.append(error)
            # Keep only last 100 errors
            if len(self.errors) > 100:
                self.errors = self.errors[-100:]

    def record_connection(self) -> None:
        with self.lock:
            self.total_connections += 1


class MockEgressProxy:
    """A TLS-intercepting proxy that forwards traffic to real destinations.

    - Requires Basic auth on CONNECT requests (like Anthropic's proxy)
    - Performs TLS interception using a mock CA
    - Actually forwards traffic to real servers (or through upstream proxy)
    - Enables e2e testing of the full proxy chain
    - Supports chaining through upstream proxy (auto-detected from HTTPS_PROXY)
    """

    def __init__(
        self,
        *,
        upstream_proxy: EgressProxyConfig | None,
        listen_port: int = 0,
        require_auth: bool = True,
        username: str = "testuser",
        password: str = "testpass",
        temp_dir: Path | None = None,
        max_workers: int = 100,
    ):
        self.listen_port = listen_port
        self.require_auth = require_auth
        self.username = username
        self.password = password
        self.temp_dir = temp_dir
        self.max_workers = max_workers
        self.upstream_proxy = upstream_proxy

        self.server_socket: socket.socket | None = None
        self.port: int = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._connections: list[socket.socket] = []

        # CA cert/key for TLS interception
        self._ca_cert_pem: bytes = b""
        self._ca_key_pem: bytes = b""

        # Cache for generated server certs (hostname -> (cert_pem, key_pem))
        self._server_certs: dict[str, tuple[bytes, bytes]] = {}
        self._cert_lock = threading.Lock()

        # Connection statistics for debugging
        self.stats = ConnectionStats()

        # Semaphore to limit concurrent outbound connections
        # This prevents overwhelming target servers when many parallel connections come in
        self._outbound_semaphore = threading.Semaphore(20)

    @property
    def ca_cert_pem(self) -> bytes:
        """Get the CA certificate PEM."""
        return self._ca_cert_pem

    @property
    def url(self) -> str:
        """Get the proxy URL with credentials."""
        return f"http://{self.username}:{self.password}@127.0.0.1:{self.port}"

    def __enter__(self) -> MockEgressProxy:
        """Start proxy and return self for context manager use."""
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object) -> None:
        """Stop proxy on context exit."""
        self.stop()

    def start(self) -> None:
        """Start the proxy server."""
        # Generate CA cert
        self._ca_cert_pem, self._ca_key_pem = generate_mock_ca()

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.listen_port))
        self.port = self.server_socket.getsockname()[1]
        self.server_socket.listen(50)  # Increased backlog for concurrent connections
        self.server_socket.settimeout(0.5)

        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="mock-proxy")
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if self.upstream_proxy:
            logger.info(
                "MockEgressProxy started on port %d (chaining through upstream %s:%d, max_workers=%d)",
                self.port,
                self.upstream_proxy.host,
                self.upstream_proxy.port,
                self.max_workers,
            )
        else:
            logger.info(
                "MockEgressProxy started on port %d (direct connections, max_workers=%d)", self.port, self.max_workers
            )

    def stop(self) -> None:
        """Stop the proxy server."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        for conn in self._connections:
            with contextlib.suppress(OSError):
                conn.close()
        if self.server_socket:
            self.server_socket.close()
        logger.info(
            "MockEgressProxy stopped. Stats: %d total, %d success, %d failed, %d bytes",
            self.stats.total_connections,
            self.stats.successful_connections,
            self.stats.failed_connections,
            self.stats.bytes_forwarded,
        )
        if self.stats.errors:
            logger.info("Recent errors: %s", self.stats.errors[-5:])

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client_sock, _addr = self.server_socket.accept()  # type: ignore[union-attr]
                self._connections.append(client_sock)
                self.stats.record_connection()
                # Use thread pool instead of spawning unlimited threads
                self._executor.submit(self._handle_client, client_sock)  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break

    def _get_server_cert(self, hostname: str) -> tuple[bytes, bytes]:
        """Get or generate a server certificate for the hostname."""
        with self._cert_lock:
            if hostname not in self._server_certs:
                self._server_certs[hostname] = generate_server_cert(self._ca_cert_pem, self._ca_key_pem, hostname)
            return self._server_certs[hostname]

    def _connect_to_target(self, target_host: str, target_port: int) -> ssl.SSLSocket:
        """Connect to target server, optionally through upstream proxy.

        Returns an SSL-wrapped socket connected to the target.
        """
        if self.upstream_proxy:
            return self._connect_via_upstream(target_host, target_port)
        return self._connect_direct(target_host, target_port)

    def _connect_direct(self, target_host: str, target_port: int) -> ssl.SSLSocket:
        """Connect directly to target server."""
        server_sock = socket.create_connection((target_host, target_port), timeout=60)
        server_sock.settimeout(60)
        server_ctx = ssl.create_default_context()
        return server_ctx.wrap_socket(server_sock, server_hostname=target_host)

    def _connect_via_upstream(self, target_host: str, target_port: int) -> ssl.SSLSocket:
        """Connect to target through upstream proxy.

        The upstream proxy (e.g., Anthropic's TLS-inspecting proxy) will:
        1. Accept our CONNECT request
        2. Establish tunnel to target
        3. Perform TLS MITM (presenting a cert signed by its CA)

        We trust the upstream CA via SSL_CERT_FILE or similar env var.
        """
        upstream = self.upstream_proxy
        assert upstream is not None

        logger.debug(
            "Connecting to %s:%d via upstream proxy %s:%d (auth: %s, ca: %s)",
            target_host,
            target_port,
            upstream.host,
            upstream.port,
            "yes" if upstream.username else "no",
            upstream.ca_bundle,
        )

        # Connect to upstream proxy
        proxy_sock = socket.create_connection((upstream.host, upstream.port), timeout=60)
        proxy_sock.settimeout(60)

        # Build CONNECT request
        connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        connect_req += f"Host: {target_host}:{target_port}\r\n"

        # Add auth if configured
        if upstream.username and upstream.password:
            creds = f"{upstream.username}:{upstream.password}"
            encoded = base64.b64encode(creds.encode()).decode()
            connect_req += f"Proxy-Authorization: Basic {encoded}\r\n"

        connect_req += "\r\n"
        proxy_sock.sendall(connect_req.encode())

        # Read response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = proxy_sock.recv(4096)
            if not chunk:
                raise ConnectionError("Upstream proxy closed connection")
            response += chunk

        # Check for success (2xx status)
        status_line = response.split(b"\r\n")[0].decode()
        if " 200 " not in status_line and " 2" not in status_line.split()[1]:
            raise ConnectionError(f"Upstream proxy rejected CONNECT: {status_line}")

        logger.debug("Upstream proxy tunnel established to %s:%d", target_host, target_port)

        # Wrap with TLS to target (upstream proxy does MITM, we trust its CA)
        server_ctx = ssl.create_default_context()
        if upstream.ca_bundle and Path(upstream.ca_bundle).exists():
            server_ctx.load_verify_locations(upstream.ca_bundle)
        else:
            # No CA bundle available - disable verification for test proxy
            # This happens in CI when HTTPS_PROXY is set but SSL_CERT_FILE is not
            logger.debug("No CA bundle for upstream proxy, disabling certificate verification")
            server_ctx.check_hostname = False
            server_ctx.verify_mode = ssl.CERT_NONE
        return server_ctx.wrap_socket(proxy_sock, server_hostname=target_host)

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
        client_ssl: ssl.SSLSocket | None = None
        server_ssl: ssl.SSLSocket | None = None
        target_host: str = "<unknown>"
        target_port: int = 0
        bytes_forwarded: int = 0

        try:
            # Set socket timeout for initial handshake
            client_sock.settimeout(60)

            # Read CONNECT request
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                request += chunk

            request_line = request.split(b"\r\n", 1)[0].decode()
            if not request_line.startswith("CONNECT "):
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                self.stats.record_failure(f"Non-CONNECT request: {request_line[:50]}")
                return

            # Parse target host:port
            parts = request_line.split()
            if len(parts) < 2:
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                self.stats.record_failure("Malformed CONNECT request")
                return

            target = parts[1]
            if ":" in target:
                target_host, port_str = target.rsplit(":", 1)
                target_port = int(port_str)
            else:
                target_host = target
                target_port = 443

            conn_id = self.stats.total_connections
            logger.info("[conn %d] CONNECT request for %s:%d", conn_id, target_host, target_port)

            # Check auth header
            if self.require_auth:
                auth_ok = False
                for line in request.split(b"\r\n"):
                    if line.lower().startswith(b"proxy-authorization: basic "):
                        encoded = line.split(b" ", 2)[2]
                        decoded = base64.b64decode(encoded).decode()
                        if ":" in decoded:
                            user, passwd = decoded.split(":", 1)
                            if user == self.username and passwd == self.password:
                                auth_ok = True
                        break

                if not auth_ok:
                    client_sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                    self.stats.record_failure(f"Auth failed for {target_host}:{target_port}")
                    return

            # Connect to real target BEFORE sending 200 (so we can return error if connection fails)
            # Use semaphore to limit concurrent outbound connections and prevent overwhelming targets
            logger.info("[conn %d] Waiting for outbound slot for %s:%d", conn_id, target_host, target_port)
            with self._outbound_semaphore:
                logger.info("[conn %d] Connecting to target %s:%d", conn_id, target_host, target_port)
                try:
                    server_ssl = self._connect_to_target(target_host, target_port)
                    logger.info("[conn %d] Connected to target %s:%d", conn_id, target_host, target_port)
                except Exception as e:
                    error_msg = f"Failed to connect to {target_host}:{target_port}: {e}"
                    logger.warning("[conn %d] %s", conn_id, error_msg)
                    self.stats.record_failure(error_msg)
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return

            # Send 200 Connection Established (only after successful connection to target)
            logger.info("[conn %d] Sending 200 to client for %s:%d", conn_id, target_host, target_port)
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Generate server cert for this hostname and wrap client connection
            # Include CA cert in chain so clients can extract it via get_unverified_chain()
            server_cert_pem, server_key_pem = self._get_server_cert(target_host)

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _load_cert_chain_from_bytes(client_ctx, server_cert_pem, server_key_pem, self._ca_cert_pem)
            client_ssl = client_ctx.wrap_socket(client_sock, server_side=True)

            # Bidirectional forward
            bytes_forwarded = self._forward_bidirectional(client_ssl, server_ssl, target_host)
            self.stats.record_success(bytes_forwarded)
            logger.info(
                "[conn %d] Completed %s:%d, %d bytes forwarded", conn_id, target_host, target_port, bytes_forwarded
            )

        except TimeoutError as e:
            error_msg = f"Timeout connecting to {target_host}:{target_port}: {e}"
            logger.warning(error_msg)
            self.stats.record_failure(error_msg)
        except ssl.SSLError as e:
            error_msg = f"SSL error for {target_host}:{target_port}: {e}"
            logger.warning(error_msg)
            self.stats.record_failure(error_msg)
        except OSError as e:
            error_msg = f"OS error for {target_host}:{target_port}: {e}"
            logger.warning(error_msg)
            self.stats.record_failure(error_msg)
        except ValueError as e:
            error_msg = f"Value error for {target_host}:{target_port}: {e}"
            logger.warning(error_msg)
            self.stats.record_failure(error_msg)
        finally:
            # Close SSL sockets first (they close underlying sockets too)
            if server_ssl:
                with contextlib.suppress(OSError):
                    server_ssl.close()
            if client_ssl:
                with contextlib.suppress(OSError):
                    client_ssl.close()
            elif client_sock:
                # Only close raw socket if SSL wrapping didn't happen
                with contextlib.suppress(OSError):
                    client_sock.close()
            # Remove from connections list to allow garbage collection
            with contextlib.suppress(ValueError):
                self._connections.remove(client_sock)

    def _forward_bidirectional(self, client_ssl: ssl.SSLSocket, server_ssl: ssl.SSLSocket, target_host: str) -> int:
        """Forward data bidirectionally between client and server.

        Returns total bytes forwarded.
        """
        sockets = [client_ssl, server_ssl]
        bytes_forwarded = 0

        # Set non-blocking for select
        client_ssl.setblocking(False)
        server_ssl.setblocking(False)

        try:
            while True:
                try:
                    readable, _, errored = select.select(sockets, [], sockets, 30.0)
                except (ValueError, OSError) as e:
                    # Socket closed during select
                    logger.warning("Select error for %s: %s", target_host, e)
                    break

                if errored:
                    logger.warning("Socket error condition for %s", target_host)
                    break

                if not readable:
                    # Timeout - check if connection is still alive
                    continue

                for sock in readable:
                    try:
                        data = sock.recv(65536)
                        if not data:
                            return bytes_forwarded  # Connection closed gracefully

                        # Forward to the other socket
                        other = server_ssl if sock is client_ssl else client_ssl
                        other.sendall(data)
                        bytes_forwarded += len(data)
                    except ssl.SSLWantReadError:
                        continue
                    except ssl.SSLWantWriteError:
                        continue
                    except (OSError, ssl.SSLError) as e:
                        logger.warning("Forward error for %s: %s", target_host, e)
                        return bytes_forwarded

        except (OSError, ssl.SSLError) as e:
            logger.warning("Bidirectional forward error for %s: %s", target_host, e)

        return bytes_forwarded


def _load_cert_chain_from_bytes(
    ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes, ca_cert_pem: bytes | None = None
) -> None:
    """Load cert chain from PEM bytes by writing to temp files.

    Python's ssl module doesn't have load_cert_chain_from_bytes, so we
    write temp files and call load_cert_chain.

    If ca_cert_pem is provided, it's appended to the cert file to send
    the full chain (required for get_unverified_chain() to return the CA).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"
        # Include CA cert in chain so clients see the full chain
        chain = cert_pem + (b"\n" + ca_cert_pem if ca_cert_pem else b"")
        cert_path.write_bytes(chain)
        key_path.write_bytes(key_pem)
        ctx.load_cert_chain(str(cert_path), str(key_path))
