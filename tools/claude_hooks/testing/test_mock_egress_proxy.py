"""Tests for MockEgressProxy test utility.

Tests cover:
- Auth enforcement (valid credentials accepted, missing/wrong rejected)
- Bidirectional TLS forwarding through the proxy with a local echo server,
  verifying data integrity for small payloads, large transfers (~8MB),
  concurrent connections, and graceful server-initiated close.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import tempfile
import threading
from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel

from tools.claude_hooks.testing.fixtures import MockEgressProxyFixture
from tools.claude_hooks.testing.mock_egress_proxy import MockEgressProxy, generate_mock_ca, generate_server_cert

# Register fixtures from module (pytest-native, no direct name import needed)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


# ---------------------------------------------------------------------------
# Helpers: local TLS echo/send server driven entirely by the test
# ---------------------------------------------------------------------------


class _TLSEchoServer:
    """Minimal TLS server that echoes data back to the client.

    Runs in a background thread so tests can drive both sides.
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port: int = 0
        self._server_socket: socket.socket | None = None
        self._ctx: ssl.SSLContext | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.ca_cert_pem: bytes = b""

    def start(self) -> None:
        ca_cert_pem, ca_key_pem = generate_mock_ca()
        self.ca_cert_pem = ca_cert_pem
        cert_pem, key_pem = generate_server_cert(ca_cert_pem, ca_key_pem, self.host)

        self._tmpdir = tempfile.mkdtemp()
        cert_path = Path(self._tmpdir) / "cert.pem"
        key_path = Path(self._tmpdir) / "key.pem"
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)

        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert_path), str(key_path))

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, 0))
        self.port = self._server_socket.getsockname()[1]
        self._server_socket.listen(10)
        self._server_socket.settimeout(1.0)

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._server_socket:
            self._server_socket.close()

    def __enter__(self) -> _TLSEchoServer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def _serve(self) -> None:
        while self._running:
            try:
                raw_sock, _ = self._server_socket.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(raw_sock,), daemon=True).start()

    def _handle(self, raw_sock: socket.socket) -> None:
        try:
            conn = self._ctx.wrap_socket(raw_sock, server_side=True)  # type: ignore[union-attr]
            conn.settimeout(10)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            raw_sock.close()


class _TLSSendServer:
    """TLS server that sends a fixed payload then closes (no reading).

    Simulates a download endpoint like releases.bazel.build.
    """

    def __init__(self, payload: bytes, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port: int = 0
        self.payload = payload
        self._server_socket: socket.socket | None = None
        self._ctx: ssl.SSLContext | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.ca_cert_pem: bytes = b""

    def start(self) -> None:
        ca_cert_pem, ca_key_pem = generate_mock_ca()
        self.ca_cert_pem = ca_cert_pem
        cert_pem, key_pem = generate_server_cert(ca_cert_pem, ca_key_pem, self.host)

        self._tmpdir = tempfile.mkdtemp()
        cert_path = Path(self._tmpdir) / "cert.pem"
        key_path = Path(self._tmpdir) / "key.pem"
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)

        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert_path), str(key_path))

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, 0))
        self.port = self._server_socket.getsockname()[1]
        self._server_socket.listen(10)
        self._server_socket.settimeout(1.0)

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._server_socket:
            self._server_socket.close()

    def __enter__(self) -> _TLSSendServer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def _serve(self) -> None:
        while self._running:
            try:
                raw_sock, _ = self._server_socket.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(raw_sock,), daemon=True).start()

    def _handle(self, raw_sock: socket.socket) -> None:
        try:
            conn = self._ctx.wrap_socket(raw_sock, server_side=True)  # type: ignore[union-attr]
            conn.settimeout(10)
            conn.sendall(self.payload)
            conn.shutdown(socket.SHUT_WR)
            # Drain any client data before closing
            while conn.recv(4096):
                pass
        except (OSError, ssl.SSLError):
            pass
        finally:
            raw_sock.close()


def _recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    """Read exactly `length` bytes from an SSL socket."""
    received = b""
    while len(received) < length:
        chunk = sock.recv(min(65536, length - len(received)))
        if not chunk:
            break
        received += chunk
    return received


def _connect_through_proxy(proxy: MockEgressProxy, target_host: str, target_port: int) -> ssl.SSLSocket:
    """Open a TLS connection to target_host:target_port through the proxy.

    Performs CONNECT handshake with auth, then wraps in TLS.
    """
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.settimeout(10)

    creds = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
    connect = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        f"Proxy-Authorization: Basic {creds}\r\n"
        f"\r\n"
    )
    sock.sendall(connect.encode())

    # Read 200
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Proxy closed during CONNECT")
        response += chunk

    if b"200" not in response.split(b"\r\n")[0]:
        raise ConnectionError(f"CONNECT failed: {response!r}")

    # Wrap in TLS trusting the proxy CA (MITM cert)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=target_host)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy_socket(mock_egress_proxy: MockEgressProxyFixture) -> Generator[socket.socket]:
    """Socket connected to the mock proxy."""
    sock = socket.create_connection(("127.0.0.1", mock_egress_proxy.proxy.port), timeout=5)
    try:
        yield sock
    finally:
        sock.close()


@pytest.fixture
def forwarding_proxy() -> Generator[MockEgressProxy]:
    """Standalone proxy for forwarding tests (no upstream).

    Disables target cert verification since test TLS servers use self-signed certs.
    """
    with MockEgressProxy(upstream_proxy=None, listen_port=0, verify_target_certs=False) as proxy:
        yield proxy


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_proxy_starts_and_stops() -> None:
    """Test basic proxy lifecycle via context manager."""
    with MockEgressProxy(upstream_proxy=None, listen_port=0) as proxy:
        assert proxy.port > 0
        assert proxy.ca_cert_pem


def test_proxy_requires_auth(proxy_socket: socket.socket) -> None:
    """Test that proxy rejects unauthenticated requests."""
    proxy_socket.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    response = proxy_socket.recv(1024)
    assert b"407" in response, f"Expected 407, got: {response!r}"


def test_proxy_accepts_auth(proxy_socket: socket.socket) -> None:
    """Test that proxy does not reject valid credentials (407).

    The proxy may return 502 if it can't reach the target (e.g., in a sandbox).
    The key assertion: it does NOT return 407 (auth rejected).
    """
    creds = base64.b64encode(b"proxy_user:test_jwt_token").decode()
    request = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {creds}\r\n\r\n"
    proxy_socket.sendall(request.encode())
    response = proxy_socket.recv(1024)
    assert b"407" not in response, f"Auth should have been accepted, got: {response!r}"


# ---------------------------------------------------------------------------
# Bidirectional forwarding tests
# ---------------------------------------------------------------------------


class TestBidirectionalForwarding:
    """Test data forwarding through the TLS-intercepting proxy.

    Each test starts a local TLS server, connects through the proxy with
    auth, and verifies data integrity on both sides.
    """

    def test_small_echo(self, forwarding_proxy: MockEgressProxy) -> None:
        """Small payload round-trips correctly."""
        with _TLSEchoServer() as echo:
            conn = _connect_through_proxy(forwarding_proxy, echo.host, echo.port)
            try:
                msg = b"hello proxy world"
                conn.sendall(msg)
                # Read exactly the expected number of bytes (SSL lacks TCP-style half-close,
                # so shutdown(SHUT_WR) would trigger close_notify and break the session)
                received = _recv_exact(conn, len(msg))
                assert received == msg
            finally:
                conn.close()

    def test_large_echo(self, forwarding_proxy: MockEgressProxy) -> None:
        """1 MB payload round-trips without data loss or corruption."""
        payload = os.urandom(1024 * 1024)
        expected_hash = hashlib.sha256(payload).hexdigest()

        with _TLSEchoServer() as echo:
            conn = _connect_through_proxy(forwarding_proxy, echo.host, echo.port)
            try:
                conn.sendall(payload)
                received = _recv_exact(conn, len(payload))
                assert len(received) == len(payload)
                assert hashlib.sha256(received).hexdigest() == expected_hash
            finally:
                conn.close()

    def test_large_download(self, forwarding_proxy: MockEgressProxy) -> None:
        """~8 MB server-initiated send (simulates bazelisk download).

        The original bug dropped data during large one-directional TLS
        transfers when sendall() raised SSLWantWriteError in non-blocking mode.
        """
        size = 8 * 1024 * 1024
        payload = os.urandom(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        with _TLSSendServer(payload) as server:
            conn = _connect_through_proxy(forwarding_proxy, server.host, server.port)
            try:
                received = b""
                while chunk := conn.recv(65536):
                    received += chunk
                assert len(received) == size, f"Expected {size} bytes, got {len(received)}"
                assert hashlib.sha256(received).hexdigest() == expected_hash
            finally:
                conn.close()

    def test_multiple_messages(self, forwarding_proxy: MockEgressProxy) -> None:
        """Multiple send/recv cycles on the same connection."""
        with _TLSEchoServer() as echo:
            conn = _connect_through_proxy(forwarding_proxy, echo.host, echo.port)
            try:
                for i in range(10):
                    msg = f"message {i} ".encode() * 100
                    conn.sendall(msg)
                    received = _recv_exact(conn, len(msg))
                    assert received == msg, f"Mismatch on message {i}"
            finally:
                conn.close()

    def test_concurrent_connections(self, forwarding_proxy: MockEgressProxy) -> None:
        """Multiple simultaneous connections each transfer data correctly."""
        results: dict[int, bool] = {}
        errors: list[str] = []

        def worker(worker_id: int) -> None:
            try:
                with _TLSEchoServer() as echo:
                    conn = _connect_through_proxy(forwarding_proxy, echo.host, echo.port)
                    try:
                        payload = os.urandom(64 * 1024)
                        conn.sendall(payload)
                        received = _recv_exact(conn, len(payload))
                        results[worker_id] = received == payload
                    finally:
                        conn.close()
            except Exception as e:
                errors.append(f"worker {worker_id}: {e}")
                results[worker_id] = False

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Worker errors: {errors}"
        assert all(results.values()), f"Failed workers: {[k for k, v in results.items() if not v]}"

    def test_server_closes_immediately(self, forwarding_proxy: MockEgressProxy) -> None:
        """Proxy handles server closing right after TLS handshake."""
        with _TLSSendServer(b"") as server:
            conn = _connect_through_proxy(forwarding_proxy, server.host, server.port)
            try:
                received = conn.recv(4096)
                assert received == b""
            finally:
                conn.close()


if __name__ == "__main__":
    pytest_bazel.main()
