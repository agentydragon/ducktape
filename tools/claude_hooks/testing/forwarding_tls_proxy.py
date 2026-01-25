"""TLS-intercepting proxy that forwards to real destinations.

Unlike MockTLSProxy which only does TLS handshake and closes, this proxy
actually forwards traffic to real servers while doing TLS MITM. This enables
e2e testing of the full proxy chain including actual BCR fetches.

Supports chaining through an upstream proxy (detected via HTTPS_PROXY env var),
which is required in environments like gVisor where direct internet access is
blocked and all traffic must go through a corporate proxy.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


@dataclass
class UpstreamProxyConfig:
    """Configuration for upstream proxy."""

    host: str
    port: int
    username: str | None = None
    password: str | None = None
    ca_bundle: str | None = None  # Path to CA bundle for verifying upstream TLS

    @classmethod
    def from_env(cls, exclude_localhost: bool = True) -> UpstreamProxyConfig | None:
        """Parse upstream proxy from environment variables.

        Looks for HTTPS_PROXY or https_proxy in format:
        http://user:pass@host:port or http://host:port

        Args:
            exclude_localhost: If True, ignore proxies pointing to localhost/127.0.0.1
                              (to avoid self-referential loops in test environments)
        """
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy_url:
            return None

        parsed = urllib.parse.urlparse(proxy_url)
        if not parsed.hostname:
            return None

        # Skip localhost proxies to avoid chaining through ourselves in tests
        if exclude_localhost and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            logger.debug("Ignoring localhost proxy %s (exclude_localhost=True)", proxy_url)
            return None

        # Get CA bundle for verifying upstream proxy's TLS
        ca_bundle = (
            os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
        )

        return cls(
            host=parsed.hostname,
            port=parsed.port or 8080,
            username=urllib.parse.unquote(parsed.username) if parsed.username else None,
            password=urllib.parse.unquote(parsed.password) if parsed.password else None,
            ca_bundle=ca_bundle,
        )


def generate_mock_ca() -> tuple[bytes, bytes]:
    """Generate a self-signed CA cert matching Anthropic's real CA format.

    The real Anthropic CA has:
    - Subject: O=Anthropic, CN=sandbox-egress-production TLS Inspection CA
    - Self-signed (issuer = subject)
    - RSA 2048-bit key
    - 10-year validity

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

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=3650))  # 10 years like real CA
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return cert_pem, key_pem


def generate_server_cert(ca_cert_pem: bytes, ca_key_pem: bytes, hostname: str) -> tuple[bytes, bytes]:
    """Generate a server certificate signed by the CA for a specific hostname.

    Matches real Anthropic proxy behavior:
    - Subject CN = target hostname (truncated to 64 chars if needed)
    - Issuer = Anthropic CA
    - 24h validity (real proxy caches and rotates multiple certs per hostname)
    - SAN with DNS name (full hostname, no length limit)

    Returns (cert_pem, key_pem) tuple.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # CN has a 64-character limit; truncate if needed (SAN is authoritative anyway)
    cn_hostname = hostname[:64] if len(hostname) > 64 else hostname
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_hostname)])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
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


class ForwardingTLSProxy:
    """A TLS-intercepting proxy that forwards traffic to real destinations.

    - Requires Basic auth on CONNECT requests (like Anthropic's proxy)
    - Performs TLS interception using a mock CA
    - Actually forwards traffic to real servers (or through upstream proxy)
    - Enables e2e testing of the full proxy chain
    - Supports chaining through upstream proxy (auto-detected from HTTPS_PROXY)
    """

    def __init__(
        self,
        listen_port: int = 0,
        require_auth: bool = True,
        username: str = "testuser",
        password: str = "testpass",
        temp_dir: Path | None = None,
        upstream_proxy: UpstreamProxyConfig | None = None,
    ):
        self.listen_port = listen_port
        self.require_auth = require_auth
        self.username = username
        self.password = password
        self.temp_dir = temp_dir

        # Auto-detect upstream proxy from environment if not explicitly provided
        self.upstream_proxy = upstream_proxy if upstream_proxy is not None else UpstreamProxyConfig.from_env()

        self.server_socket: socket.socket | None = None
        self.port: int = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._connections: list[socket.socket] = []

        # CA cert/key for TLS interception
        self._ca_cert_pem: bytes = b""
        self._ca_key_pem: bytes = b""

        # Cache for generated server certs (hostname -> (cert_pem, key_pem))
        self._server_certs: dict[str, tuple[bytes, bytes]] = {}
        self._cert_lock = threading.Lock()

        # Connection statistics for debugging
        self.stats = ConnectionStats()

    @property
    def ca_cert_pem(self) -> bytes:
        """Get the CA certificate PEM."""
        return self._ca_cert_pem

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

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if self.upstream_proxy:
            logger.info(
                "ForwardingTLSProxy started on port %d (chaining through upstream %s:%d)",
                self.port,
                self.upstream_proxy.host,
                self.upstream_proxy.port,
            )
        else:
            logger.info("ForwardingTLSProxy started on port %d (direct connections)", self.port)

    def stop(self) -> None:
        """Stop the proxy server."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        for conn in self._connections:
            with contextlib.suppress(OSError):
                conn.close()
        if self.server_socket:
            self.server_socket.close()
        logger.info(
            "ForwardingTLSProxy stopped. Stats: %d total, %d success, %d failed, %d bytes",
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
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
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

            logger.debug("CONNECT %s:%d", target_host, target_port)

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

            # Send 200 Connection Established
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Connect to real target (directly or via upstream proxy)
            server_ssl = self._connect_to_target(target_host, target_port)

            # Generate server cert for this hostname and wrap client connection
            # Include CA cert in chain so clients can extract it via get_unverified_chain()
            server_cert_pem, server_key_pem = self._get_server_cert(target_host)

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _load_cert_chain_from_bytes(client_ctx, server_cert_pem, server_key_pem, self._ca_cert_pem)
            client_ssl = client_ctx.wrap_socket(client_sock, server_side=True)

            # Bidirectional forward
            bytes_forwarded = self._forward_bidirectional(client_ssl, server_ssl, target_host)
            self.stats.record_success(bytes_forwarded)
            logger.debug("Connection to %s:%d completed, %d bytes forwarded", target_host, target_port, bytes_forwarded)

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
            for sock in [client_ssl, server_ssl, client_sock]:
                if sock:
                    with contextlib.suppress(OSError):
                        sock.close()

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
                    logger.debug("Select error for %s: %s", target_host, e)
                    break

                if errored:
                    logger.debug("Socket error condition for %s", target_host)
                    break

                if not readable:
                    # Timeout - check if connection is still alive
                    continue

                for sock in readable:
                    try:
                        data = sock.recv(65536)  # Larger buffer for efficiency
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
                        logger.debug("Forward error for %s: %s", target_host, e)
                        return bytes_forwarded

        except (OSError, ssl.SSLError) as e:
            logger.debug("Bidirectional forward error for %s: %s", target_host, e)

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
