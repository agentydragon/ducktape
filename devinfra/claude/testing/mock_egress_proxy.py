"""Mock egress proxy for testing.

Simulates the behavior of Anthropic's TLS-inspecting egress proxy:
- Requires Basic auth on CONNECT requests
- Performs TLS interception with a mock CA matching real CA format
- Forwards traffic to real destinations (or chains through upstream proxy)

Used for e2e testing of the session_start hook and auth proxy infrastructure.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import ssl
import tempfile
import urllib.parse
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import BaseModel

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS
from devinfra.claude.auth_proxy.vars import get_upstream_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class EgressProxyConfig:
    """Configuration for upstream proxy."""

    host: str
    port: int
    username: str | None = None
    password: str | None = None
    ca_bundle: Path | None = None

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

        # Get CA bundle for verifying upstream proxy's TLS interception cert.
        ca_bundle_str = next((v for var in SSL_CA_ENV_VARS if (v := os.environ.get(var))), None)
        ca_bundle = Path(ca_bundle_str) if ca_bundle_str else None

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


class ConnectionRecord(BaseModel):
    """A single proxied connection."""

    method: str
    host: str
    port: int
    success: bool
    bytes_forwarded: int = 0
    error: str | None = None


class ConnectionStats(BaseModel):
    """Track connection statistics for debugging."""

    total_connections: int = 0
    successful_connections: int = 0
    failed_connections: int = 0
    bytes_forwarded: int = 0
    errors: list[str] = []
    connections: list[ConnectionRecord] = []

    def record_success(self, bytes_count: int = 0) -> None:
        self.successful_connections += 1
        self.bytes_forwarded += bytes_count

    def record_failure(self, error: str) -> None:
        self.failed_connections += 1
        self.errors.append(error)
        if len(self.errors) > 100:
            self.errors = self.errors[-100:]

    def record_connection(self) -> None:
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
        listen_address: str = "127.0.0.1",
        username: str = "testuser",
        password: str = "testpass",
        max_concurrent_outbound: int = 20,
        verify_target_certs: bool = True,
    ):
        self.listen_port = listen_port
        self.listen_address = listen_address
        self.username = username
        self.password = password
        self.upstream_proxy = upstream_proxy
        self.verify_target_certs = verify_target_certs

        self._server: asyncio.Server | None = None
        self.port: int = 0
        self._tasks: set[asyncio.Task[None]] = set()

        self._ca_cert_pem, self._ca_key_pem = generate_mock_ca()

        # Cache for generated server certs (hostname -> (cert_pem, key_pem))
        self._server_certs: dict[str, tuple[bytes, bytes]] = {}

        self.stats = ConnectionStats()
        self._outbound_semaphore = asyncio.Semaphore(max_concurrent_outbound)

    @property
    def ca_cert_pem(self) -> bytes:
        return self._ca_cert_pem

    @property
    def url(self) -> str:
        return f"http://{self.username}:{self.password}@127.0.0.1:{self.port}"

    async def __aenter__(self) -> MockEgressProxy:
        self._server = await asyncio.start_server(self._on_connection, self.listen_address, self.listen_port)
        addrs = self._server.sockets
        assert addrs, "Server has no sockets"
        self.port = addrs[0].getsockname()[1]

        if self.upstream_proxy:
            logger.info(
                "MockEgressProxy started on port %d (chaining through upstream %s:%d)",
                self.port,
                self.upstream_proxy.host,
                self.upstream_proxy.port,
            )
        else:
            logger.info("MockEgressProxy started on port %d (direct connections)", self.port)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info(
            "MockEgressProxy stopped. Stats: %d total, %d success, %d failed, %d bytes",
            self.stats.total_connections,
            self.stats.successful_connections,
            self.stats.failed_connections,
            self.stats.bytes_forwarded,
        )
        if self.stats.errors:
            logger.info("Recent errors: %s", self.stats.errors[-5:])

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Callback for asyncio.start_server — wraps _handle_client in a tracked task."""
        self.stats.record_connection()
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            self._tasks.discard(task)

    async def _send_error(self, writer: asyncio.StreamWriter, response: bytes, error: str) -> None:
        writer.write(response)
        await writer.drain()
        self.stats.record_failure(error)

    async def _require_auth(self, writer: asyncio.StreamWriter, request: bytes, context: str) -> bool:
        """Check Proxy-Authorization header, send 407 if it fails. Returns True if auth passed."""
        authenticated = False
        for line in request.split(b"\r\n"):
            if line.lower().startswith(b"proxy-authorization: basic "):
                encoded = line.split(b" ", 2)[2]
                decoded = base64.b64decode(encoded).decode()
                if ":" in decoded:
                    user, passwd = decoded.split(":", 1)
                    authenticated = user == self.username and passwd == self.password
                break
        if authenticated:
            return True
        await self._send_error(
            writer, b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n", f"Auth failed for {context}"
        )
        return False

    async def _connect_or_reject(
        self, writer: asyncio.StreamWriter, target_host: str, target_port: int, conn_id: int, *, use_tls: bool = True
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Connect to target with semaphore + timeout, send 502 on failure. Returns None on error."""
        async with self._outbound_semaphore:
            logger.info("[conn %d] Connecting to target %s:%d", conn_id, target_host, target_port)
            try:
                if not self.upstream_proxy:
                    if use_tls:
                        ctx = self._create_client_ssl_context(ca_bundle=None)
                        result = await asyncio.wait_for(
                            asyncio.open_connection(target_host, target_port, ssl=ctx), timeout=60
                        )
                    else:
                        result = await asyncio.wait_for(asyncio.open_connection(target_host, target_port), timeout=60)
                elif not use_tls:
                    # Plain HTTP: open TCP to upstream, caller sends absolute-URL request
                    result = await asyncio.wait_for(
                        asyncio.open_connection(self.upstream_proxy.host, self.upstream_proxy.port), timeout=60
                    )
                else:
                    # TLS via upstream: CONNECT tunnel, then TLS upgrade
                    result = await asyncio.wait_for(
                        self._connect_via_upstream_tunnel(target_host, target_port, self.upstream_proxy), timeout=60
                    )
                logger.info("[conn %d] Connected to target %s:%d", conn_id, target_host, target_port)
                return result
            except Exception as e:
                error_msg = f"Failed to connect to {target_host}:{target_port}: {e}"
                logger.warning("[conn %d] %s", conn_id, error_msg)
                await self._send_error(writer, b"HTTP/1.1 502 Bad Gateway\r\n\r\n", error_msg)
                return None

    def _create_client_ssl_context(self, *, ca_bundle: Path | None) -> ssl.SSLContext:
        """Create an SSL context for outbound connections to targets or upstream proxies."""
        ctx = ssl.create_default_context()
        if ca_bundle:
            ctx.load_verify_locations(ca_bundle)
            return ctx
        if self.verify_target_certs:
            return ctx
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _connect_via_upstream_tunnel(
        self, target_host: str, target_port: int, upstream: EgressProxyConfig
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """CONNECT tunnel through upstream proxy with TLS upgrade."""
        logger.debug(
            "Connecting to %s:%d via upstream proxy %s:%d (auth: %s, ca: %s)",
            target_host,
            target_port,
            upstream.host,
            upstream.port,
            "yes" if upstream.username else "no",
            upstream.ca_bundle,
        )

        proxy_reader, proxy_writer = await asyncio.open_connection(upstream.host, upstream.port)
        try:
            connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            connect_req += f"Host: {target_host}:{target_port}\r\n"
            if upstream.username and upstream.password:
                creds = f"{upstream.username}:{upstream.password}"
                encoded = base64.b64encode(creds.encode()).decode()
                connect_req += f"Proxy-Authorization: Basic {encoded}\r\n"
            connect_req += "\r\n"
            proxy_writer.write(connect_req.encode())
            await proxy_writer.drain()

            try:
                response = await proxy_reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError as e:
                raise ConnectionError("Upstream proxy closed connection") from e

            status_line = response.split(b"\r\n")[0].decode()
            if " 200 " not in status_line and " 2" not in status_line.split()[1]:
                raise ConnectionError(f"Upstream proxy rejected CONNECT: {status_line}")

            logger.debug("Upstream proxy tunnel established to %s:%d", target_host, target_port)

            ctx = self._create_client_ssl_context(ca_bundle=upstream.ca_bundle)
            await proxy_writer.start_tls(ctx, server_hostname=target_host)
            return proxy_reader, proxy_writer
        except BaseException:
            proxy_writer.close()
            raise

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target_host = "<unknown>"
        target_port = 0

        try:
            async with _close_writer(writer):
                # Read HTTP request headers
                try:
                    request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=60)
                except (TimeoutError, asyncio.IncompleteReadError):
                    return

                request_line = request.split(b"\r\n", 1)[0].decode()

                if request_line.startswith("CONNECT "):
                    await self._handle_connect(reader, writer, request, request_line)
                else:
                    await self._handle_plain_http(reader, writer, request, request_line)

        except asyncio.CancelledError:
            raise
        except (TimeoutError, ssl.SSLError, OSError, ValueError) as e:
            self.stats.record_failure(f"{target_host}:{target_port}: {e}")

    async def _handle_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: bytes, request_line: str
    ) -> None:
        """Handle CONNECT tunneling (TLS interception)."""
        parts = request_line.split()
        if len(parts) < 2:
            await self._send_error(writer, b"HTTP/1.1 400 Bad Request\r\n\r\n", "Malformed CONNECT request")
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

        if not await self._require_auth(writer, request, f"{target_host}:{target_port}"):
            return

        result = await self._connect_or_reject(writer, target_host, target_port, conn_id)
        if not result:
            return
        server_reader, server_writer = result

        async with _close_writer(server_writer):
            # Send 200 Connection Established (only after successful target connection)
            logger.info("[conn %d] Sending 200 to client for %s:%d", conn_id, target_host, target_port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            # Generate server cert and upgrade client connection to TLS (server-side)
            if target_host not in self._server_certs:
                self._server_certs[target_host] = generate_server_cert(self._ca_cert_pem, self._ca_key_pem, target_host)
            server_cert_pem, server_key_pem = self._server_certs[target_host]
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _load_cert_chain_from_bytes(client_ctx, server_cert_pem, server_key_pem, self._ca_cert_pem)
            await writer.start_tls(client_ctx)

            await self._relay_and_record(
                reader,
                writer,
                server_reader,
                server_writer,
                method="CONNECT",
                host=target_host,
                port=target_port,
                conn_id=conn_id,
            )

    async def _handle_plain_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: bytes, request_line: str
    ) -> None:
        """Handle plain HTTP proxy requests (GET http://host/path, etc.)."""
        parts = request_line.split()
        if len(parts) < 3:
            await self._send_error(writer, b"HTTP/1.1 400 Bad Request\r\n\r\n", "Malformed HTTP request")
            return

        method, url, http_version = parts[0], parts[1], parts[2]

        parsed = urllib.parse.urlparse(url)
        if not parsed.hostname:
            await self._send_error(writer, b"HTTP/1.1 400 Bad Request\r\n\r\n", f"Non-absolute URL: {url[:80]}")
            return

        target_host = parsed.hostname
        target_port = parsed.port or 80

        conn_id = self.stats.total_connections
        logger.info("[conn %d] %s %s (plain HTTP proxy)", conn_id, method, url[:120])

        if not await self._require_auth(writer, request, f"{method} {target_host}:{target_port}"):
            return

        result = await self._connect_or_reject(writer, target_host, target_port, conn_id, use_tls=False)
        if not result:
            return
        server_reader, server_writer = result

        async with _close_writer(server_writer):
            # Rewrite absolute URL to relative path for direct connections.
            # When chaining through an upstream proxy, keep the absolute URL.
            if self.upstream_proxy:
                rewritten_request_line = request_line
            else:
                relative_path = parsed.path or "/"
                if parsed.query:
                    relative_path += f"?{parsed.query}"
                rewritten_request_line = f"{method} {relative_path} {http_version}"

            # Rebuild headers, stripping Proxy-Authorization (not forwarded to origin)
            header_lines: list[str] = []
            for line in request.split(b"\r\n")[1:]:
                if not line:
                    break
                if line.lower().startswith(b"proxy-authorization:"):
                    continue
                header_lines.append(line.decode())

            forwarded_request = rewritten_request_line + "\r\n" + "\r\n".join(header_lines) + "\r\n\r\n"
            server_writer.write(forwarded_request.encode())
            await server_writer.drain()

            await self._relay_and_record(
                reader,
                writer,
                server_reader,
                server_writer,
                method=method,
                host=target_host,
                port=target_port,
                conn_id=conn_id,
            )

    async def _relay_and_record(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
        *,
        method: str,
        host: str,
        port: int,
        conn_id: int,
    ) -> None:
        """Bidirectional relay + stats recording + connection logging."""

        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter, direction: str) -> int:
            count = 0
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                    count += len(data)
            except (OSError, ssl.SSLError, ConnectionError) as e:
                logger.debug("Forward %s finished for %s: %s", direction, host, e)
            return count

        c2s = asyncio.create_task(forward(client_reader, server_writer, "c2s"))
        s2c = asyncio.create_task(forward(server_reader, client_writer, "s2c"))

        done, pending = await asyncio.wait([c2s, s2c], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        bytes_forwarded = sum(t.result() for t in done if not t.cancelled() and t.exception() is None)
        self.stats.record_success(bytes_forwarded)
        record = ConnectionRecord(method=method, host=host, port=port, success=True, bytes_forwarded=bytes_forwarded)
        self.stats.connections.append(record)
        logger.info("[conn %d] %s", conn_id, record.model_dump_json())


@contextlib.asynccontextmanager
async def _close_writer(writer: asyncio.StreamWriter) -> AsyncGenerator[None]:
    try:
        yield
    finally:
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()


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
