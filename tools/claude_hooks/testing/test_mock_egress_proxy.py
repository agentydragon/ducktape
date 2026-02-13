"""Tests for MockEgressProxy test utility.

Tests cover:
- Auth enforcement (valid credentials accepted, missing/wrong rejected)
- Bidirectional TLS forwarding through the proxy with a local echo server,
  verifying data integrity for small payloads, large transfers (~8MB),
  concurrent connections, and graceful server-initiated close.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import ssl
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from tools.claude_hooks.testing.fixtures import MockEgressProxyFixture
from tools.claude_hooks.testing.mock_egress_proxy import MockEgressProxy, generate_mock_ca, generate_server_cert

# Register fixtures from module (pytest-native, no direct name import needed)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


# ---------------------------------------------------------------------------
# Helpers: local TLS test servers using asyncio
# ---------------------------------------------------------------------------

_Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@dataclass
class _TLSServer:
    """A running TLS test server."""

    host: str
    port: int
    ca_cert_pem: bytes
    _server: asyncio.Server


@contextlib.asynccontextmanager
async def _tls_server(handler: _Handler, host: str = "127.0.0.1") -> AsyncGenerator[_TLSServer]:
    """Start a TLS server with a self-signed cert, yield it, then shut down."""
    ca_cert_pem, ca_key_pem = generate_mock_ca()
    cert_pem, key_pem = generate_server_cert(ca_cert_pem, ca_key_pem, host)

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(str(cert_path), str(key_path))

        server = await asyncio.start_server(handler, host, 0, ssl=ssl_ctx)
        addrs = server.sockets
        assert addrs
        port = addrs[0].getsockname()[1]
        try:
            yield _TLSServer(host=host, port=port, ca_cert_pem=ca_cert_pem, _server=server)
        finally:
            server.close()
            await server.wait_closed()


async def _echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Echo data back to the client."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, ssl.SSLError):
        pass
    finally:
        writer.close()


def _send_handler(payload: bytes) -> _Handler:
    """Return a handler that sends a fixed payload then closes."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(payload)
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
            else:
                return  # can't write EOF; finally closes the writer
            while await reader.read(4096):
                pass
        except (OSError, ssl.SSLError):
            pass
        finally:
            writer.close()

    return handler


async def _recv_exact(reader: asyncio.StreamReader, length: int) -> bytes:
    """Read exactly `length` bytes from an asyncio stream."""
    received = b""
    while len(received) < length:
        chunk = await reader.read(min(65536, length - len(received)))
        if not chunk:
            break
        received += chunk
    return received


@contextlib.asynccontextmanager
async def _connect_through_proxy(
    proxy: MockEgressProxy, target_host: str, target_port: int
) -> AsyncGenerator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """Open a TLS connection to target_host:target_port through the proxy.

    Performs CONNECT handshake with auth, upgrades to TLS, and closes the
    writer on exit.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        creds = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
        connect = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"Proxy-Authorization: Basic {creds}\r\n"
            f"\r\n"
        )
        writer.write(connect.encode())
        await writer.drain()

        try:
            response = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as e:
            raise ConnectionError("Proxy closed during CONNECT") from e

        if b"200" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"CONNECT failed: {response!r}")

        # Upgrade to TLS trusting the proxy CA (MITM cert)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        await writer.start_tls(ctx, server_hostname=target_host)
        yield reader, writer
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def proxy_connection(
    mock_egress_proxy: MockEgressProxyFixture,
) -> AsyncGenerator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """Reader/writer connected to the mock proxy (plain TCP, no TLS upgrade)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", mock_egress_proxy.proxy.port)
    try:
        yield reader, writer
    finally:
        writer.close()


@pytest.fixture
async def forwarding_proxy() -> AsyncGenerator[MockEgressProxy]:
    """Standalone proxy for forwarding tests (no upstream).

    Disables target cert verification since test TLS servers use self-signed certs.
    """
    async with MockEgressProxy(upstream_proxy=None, listen_port=0, verify_target_certs=False) as proxy:
        yield proxy


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


async def test_proxy_starts_and_stops() -> None:
    """Test basic proxy lifecycle via async context manager."""
    async with MockEgressProxy(upstream_proxy=None, listen_port=0) as proxy:
        assert proxy.port > 0
        assert proxy.ca_cert_pem


async def test_proxy_requires_auth(proxy_connection: tuple[asyncio.StreamReader, asyncio.StreamWriter]) -> None:
    """Test that proxy rejects unauthenticated requests."""
    reader, writer = proxy_connection
    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    await writer.drain()
    response = await reader.read(1024)
    assert b"407" in response, f"Expected 407, got: {response!r}"


async def test_proxy_accepts_auth(proxy_connection: tuple[asyncio.StreamReader, asyncio.StreamWriter]) -> None:
    """Test that proxy does not reject valid credentials (407).

    The proxy may return 502 if it can't reach the target (e.g., in a sandbox).
    The key assertion: it does NOT return 407 (auth rejected).
    """
    _reader, writer = proxy_connection
    creds = base64.b64encode(b"proxy_user:test_jwt_token").decode()
    request = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {creds}\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    response = await _reader.read(1024)
    assert b"407" not in response, f"Auth should have been accepted, got: {response!r}"


# ---------------------------------------------------------------------------
# Bidirectional forwarding tests
# ---------------------------------------------------------------------------


class TestBidirectionalForwarding:
    """Test data forwarding through the TLS-intercepting proxy.

    Each test starts a local TLS server, connects through the proxy with
    auth, and verifies data integrity on both sides.
    """

    async def test_small_echo(self, forwarding_proxy: MockEgressProxy) -> None:
        """Small payload round-trips correctly."""
        async with (
            _tls_server(_echo_handler) as echo,
            _connect_through_proxy(forwarding_proxy, echo.host, echo.port) as (reader, writer),
        ):
            msg = b"hello proxy world"
            writer.write(msg)
            await writer.drain()
            received = await _recv_exact(reader, len(msg))
            assert received == msg

    async def test_large_echo(self, forwarding_proxy: MockEgressProxy) -> None:
        """1 MB payload round-trips without data loss or corruption."""
        payload = os.urandom(1024 * 1024)
        expected_hash = hashlib.sha256(payload).hexdigest()

        async with (
            _tls_server(_echo_handler) as echo,
            _connect_through_proxy(forwarding_proxy, echo.host, echo.port) as (reader, writer),
        ):
            writer.write(payload)
            await writer.drain()
            received = await _recv_exact(reader, len(payload))
            assert len(received) == len(payload)
            assert hashlib.sha256(received).hexdigest() == expected_hash

    async def test_large_download(self, forwarding_proxy: MockEgressProxy) -> None:
        """~8 MB server-initiated send (simulates bazelisk download).

        The original bug dropped data during large one-directional TLS
        transfers when sendall() raised SSLWantWriteError in non-blocking mode.
        """
        size = 8 * 1024 * 1024
        payload = os.urandom(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        async with (
            _tls_server(_send_handler(payload)) as server,
            _connect_through_proxy(forwarding_proxy, server.host, server.port) as (reader, _writer),
        ):
            received = b""
            while chunk := await reader.read(65536):
                received += chunk
            assert len(received) == size, f"Expected {size} bytes, got {len(received)}"
            assert hashlib.sha256(received).hexdigest() == expected_hash

    async def test_multiple_messages(self, forwarding_proxy: MockEgressProxy) -> None:
        """Multiple send/recv cycles on the same connection."""
        async with (
            _tls_server(_echo_handler) as echo,
            _connect_through_proxy(forwarding_proxy, echo.host, echo.port) as (reader, writer),
        ):
            for i in range(10):
                msg = f"message {i} ".encode() * 100
                writer.write(msg)
                await writer.drain()
                received = await _recv_exact(reader, len(msg))
                assert received == msg, f"Mismatch on message {i}"

    async def test_concurrent_connections(self, forwarding_proxy: MockEgressProxy) -> None:
        """Multiple simultaneous connections each transfer data correctly."""
        results: dict[int, bool] = {}
        errors: list[str] = []

        async def worker(worker_id: int) -> None:
            try:
                async with (
                    _tls_server(_echo_handler) as echo,
                    _connect_through_proxy(forwarding_proxy, echo.host, echo.port) as (reader, writer),
                ):
                    payload = os.urandom(64 * 1024)
                    writer.write(payload)
                    await writer.drain()
                    received = await _recv_exact(reader, len(payload))
                    results[worker_id] = received == payload
            except Exception as e:
                errors.append(f"worker {worker_id}: {e}")
                results[worker_id] = False

        await asyncio.gather(*(worker(i) for i in range(5)))

        assert not errors, f"Worker errors: {errors}"
        assert all(results.values()), f"Failed workers: {[k for k, v in results.items() if not v]}"

    async def test_server_closes_immediately(self, forwarding_proxy: MockEgressProxy) -> None:
        """Proxy handles server closing right after TLS handshake."""
        async with (
            _tls_server(_send_handler(b"")) as server,
            _connect_through_proxy(forwarding_proxy, server.host, server.port) as (reader, _writer),
        ):
            received = await reader.read(4096)
            assert received == b""


if __name__ == "__main__":
    pytest_bazel.main()
