"""TLS-intercepting proxy that forwards to real destinations.

Unlike MockTLSProxy which only does TLS handshake and closes, this proxy
actually forwards traffic to real servers while doing TLS MITM. This enables
e2e testing of the full proxy chain including actual BCR fetches.
"""

from __future__ import annotations

import base64
import contextlib
import select
import socket
import ssl
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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
    - Subject CN = target hostname
    - Issuer = Anthropic CA
    - 24h validity (real proxy caches and rotates multiple certs per hostname)
    - SAN with DNS name

    Returns (cert_pem, key_pem) tuple.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])

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


class ForwardingTLSProxy:
    """A TLS-intercepting proxy that forwards traffic to real destinations.

    - Requires Basic auth on CONNECT requests (like Anthropic's proxy)
    - Performs TLS interception using a mock CA
    - Actually forwards traffic to real servers
    - Enables e2e testing of the full proxy chain
    """

    def __init__(
        self,
        listen_port: int = 0,
        require_auth: bool = True,
        username: str = "testuser",
        password: str = "testpass",
        temp_dir: Path | None = None,
    ):
        self.listen_port = listen_port
        self.require_auth = require_auth
        self.username = username
        self.password = password
        self.temp_dir = temp_dir

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
        self.server_socket.listen(10)
        self.server_socket.settimeout(0.5)

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

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

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client_sock, _ = self.server_socket.accept()  # type: ignore[union-attr]
                self._connections.append(client_sock)
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

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
        server_sock: socket.socket | None = None
        client_ssl: ssl.SSLSocket | None = None
        server_ssl: ssl.SSLSocket | None = None

        try:
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
                return

            # Parse target host:port
            parts = request_line.split()
            if len(parts) < 2:
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            target = parts[1]
            if ":" in target:
                target_host, port_str = target.rsplit(":", 1)
                target_port = int(port_str)
            else:
                target_host = target
                target_port = 443

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
                    return

            # Send 200 Connection Established
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Connect to real target
            server_sock = socket.create_connection((target_host, target_port), timeout=30)

            # Wrap server connection with TLS (to real server)
            server_ctx = ssl.create_default_context()
            server_ssl = server_ctx.wrap_socket(server_sock, server_hostname=target_host)

            # Generate server cert for this hostname and wrap client connection
            # Include CA cert in chain so clients can extract it via get_unverified_chain()
            server_cert_pem, server_key_pem = self._get_server_cert(target_host)

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _load_cert_chain_from_bytes(client_ctx, server_cert_pem, server_key_pem, self._ca_cert_pem)
            client_ssl = client_ctx.wrap_socket(client_sock, server_side=True)

            # Bidirectional forward
            self._forward_bidirectional(client_ssl, server_ssl)

        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            for sock in [client_ssl, server_ssl, client_sock, server_sock]:
                if sock:
                    with contextlib.suppress(OSError):
                        sock.close()

    def _forward_bidirectional(self, client_ssl: ssl.SSLSocket, server_ssl: ssl.SSLSocket) -> None:
        """Forward data bidirectionally between client and server."""
        sockets = [client_ssl, server_ssl]

        try:
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)

                if errored:
                    break

                for sock in readable:
                    try:
                        data = sock.recv(8192)
                        if not data:
                            return  # Connection closed

                        # Forward to the other socket
                        other = server_ssl if sock is client_ssl else client_ssl
                        other.sendall(data)
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue
                    except (OSError, ssl.SSLError):
                        return

        except (OSError, ssl.SSLError):
            pass


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
