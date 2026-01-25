"""Integration tests for claude_hooks proxy infrastructure.

These tests use REAL processes (supervisor, pproxy) and a mock TLS-inspecting proxy
to verify end-to-end behavior.
"""

from __future__ import annotations

import base64
import contextlib
import socket
import ssl
import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from net_util.net import pick_free_port, wait_for_port
from runfiles import get_required_path
from tools.claude_hooks import proxy_setup, settings
from tools.claude_hooks.proxy_setup import BAZEL_PROXY_SERVICE
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import is_running as supervisor_is_running
from tools.claude_hooks.supervisor.setup import start as supervisor_start
from tools.claude_hooks.testing import runfiles_util
from tools.claude_hooks.testing.supervisor_cleanup import supervisor_cleanup


@pytest.fixture(scope="session")
def mock_ca_cert() -> tuple[bytes, bytes]:
    """Generate a self-signed CA cert with 'Anthropic' in subject.

    Returns (cert_pem, key_pem) tuple.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Must match _is_anthropic_tls_inspection_ca(): org == "Anthropic" AND "TLS Inspection CA" in cn
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Anthropic"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Mock TLS Inspection CA"),
        ]
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return cert_pem, key_pem


class MockTLSProxy:
    """A mock TLS-inspecting proxy for integration testing.

    - Requires Basic auth on CONNECT requests
    - Performs TLS interception using the mock CA
    - Presents cert chain with 'Anthropic' in subject
    """

    def __init__(self, cert_path: Path, key_path: Path, expected_user: str, expected_pass: str):
        self.cert_path = cert_path
        self.key_path = key_path
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.server_socket: socket.socket | None = None
        self.port: int = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._connections: list[socket.socket] = []

    def start(self) -> None:
        """Start the mock proxy server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", 0))
        self.port = self.server_socket.getsockname()[1]
        self.server_socket.listen(5)
        self.server_socket.settimeout(0.5)  # Allow checking _running periodically

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the mock proxy server."""
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

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
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

            # Check auth header
            auth_ok = False
            for line in request.split(b"\r\n"):
                if line.lower().startswith(b"proxy-authorization: basic "):
                    encoded = line.split(b" ", 2)[2]
                    decoded = base64.b64decode(encoded).decode()
                    if ":" in decoded:
                        user, passwd = decoded.split(":", 1)
                        if user == self.expected_user and passwd == self.expected_pass:
                            auth_ok = True
                    break

            if not auth_ok:
                client_sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return

            # Send 200 Connection Established
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Wrap in TLS as intercepting proxy (present our mock cert)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.cert_path, self.key_path)
            ssl_sock = ssl_context.wrap_socket(client_sock, server_side=True)

            # Just wait for the client to close or read some data
            # (the CA extraction code just does handshake and reads cert chain)
            try:
                ssl_sock.recv(4096)
            except ssl.SSLError:
                pass
            finally:
                ssl_sock.close()

        except (OSError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(OSError):
                client_sock.close()


@pytest.fixture(scope="session")
def mock_tls_proxy(
    mock_ca_cert: tuple[bytes, bytes], tmp_path_factory: pytest.TempPathFactory
) -> Generator[MockTLSProxy]:
    """Fixture that provides a running mock TLS proxy."""
    cert_pem, key_pem = mock_ca_cert
    tmp_dir = tmp_path_factory.mktemp("mock_proxy")
    cert_path = tmp_dir / "cert.pem"
    key_path = tmp_dir / "key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    proxy = MockTLSProxy(cert_path, key_path, expected_user="testuser", expected_pass="testpass")
    proxy.start()
    yield proxy
    proxy.stop()


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Fixture that sets up isolated directories for testing.

    Returns (supervisor_dir, bazel_proxy_dir) tuple.
    """
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    bazel_proxy_dir = tmp_path / "bazel-proxy"
    bazel_proxy_dir.mkdir()

    supervisor_port = pick_free_port()
    proxy_port = pick_free_port()

    monkeypatch.setenv(settings.ENV_SUPERVISOR_DIR, str(supervisor_dir))
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(supervisor_port))
    monkeypatch.setenv(settings.ENV_BAZEL_PROXY_DIR, str(bazel_proxy_dir))
    monkeypatch.setenv(settings.ENV_BAZEL_PROXY_PORT, str(proxy_port))
    monkeypatch.setenv(settings.ENV_AUTH_PROXY_CMD, str(get_required_path(runfiles_util.RUN_AUTH_PROXY)))

    return supervisor_dir, bazel_proxy_dir


@pytest.fixture
def hook_settings(
    isolated_dirs: tuple[Path, Path], mock_tls_proxy: MockTLSProxy, monkeypatch: pytest.MonkeyPatch
) -> HookSettings:
    """Fixture that creates HookSettings with upstream proxy configured."""
    proxy_url = f"http://testuser:testpass@127.0.0.1:{mock_tls_proxy.port}"
    monkeypatch.setenv("https_proxy", proxy_url)
    return HookSettings()


@pytest.fixture(autouse=True)
def cleanup_supervisor_fixture(isolated_dirs: tuple[Path, Path]) -> Generator[None]:
    """Fixture that ensures supervisor is stopped before and after test."""
    supervisor_dir, _ = isolated_dirs
    with supervisor_cleanup(supervisor_dir / "supervisord.pid"):
        yield


class TestProxySetup:
    """Integration tests for proxy setup."""

    def test_supervisor_starts_and_proxy_runs(self, hook_settings: HookSettings) -> None:
        """Test that setup_bazel_proxy starts supervisor and proxy service."""
        supervisor_result = supervisor_start(hook_settings)
        proxy_setup.ensure_proxy_running(hook_settings, supervisor_result.client)

        assert supervisor_is_running(hook_settings), "Supervisor should be running"
        assert supervisor_result.client.is_service_running(BAZEL_PROXY_SERVICE), "bazel-proxy service should be running"
        wait_for_port("127.0.0.1", hook_settings.get_bazel_proxy_port(), timeout_secs=5)

    def test_ca_extraction(self, hook_settings: HookSettings) -> None:
        """Test that CA certificate is extracted from TLS chain."""
        supervisor_result = supervisor_start(hook_settings)
        proxy_setup.ensure_proxy_running(hook_settings, supervisor_result.client)
        wait_for_port("127.0.0.1", hook_settings.get_bazel_proxy_port(), timeout_secs=5)

        proxy_setup._extract_proxy_ca(hook_settings)

        ca_file = hook_settings.get_bazel_ca_file()
        assert ca_file.exists(), "CA file should be created"

        ca_content = ca_file.read_text()
        assert "BEGIN CERTIFICATE" in ca_content

        cert = x509.load_pem_x509_certificate(ca_content.encode())
        cn_value = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        cn = cn_value if isinstance(cn_value, str) else cn_value.decode()
        assert "TLS Inspection CA" in cn, f"Expected 'TLS Inspection CA' in CN, got: {cn}"

    def test_credential_rotation(
        self, hook_settings: HookSettings, mock_tls_proxy: MockTLSProxy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that credential changes are written to file (hot-reload)."""
        supervisor_result = supervisor_start(hook_settings)
        client = supervisor_result.client
        proxy_setup.ensure_proxy_running(hook_settings, client)
        wait_for_port("127.0.0.1", hook_settings.get_bazel_proxy_port(), timeout_secs=5)

        creds_file = hook_settings.get_bazel_creds_file()
        assert creds_file.exists(), "Creds file should exist"
        assert "testuser" in creds_file.read_text(), "Initial creds should have original credentials"

        new_proxy_url = f"http://newuser:newpass@127.0.0.1:{mock_tls_proxy.port}"
        monkeypatch.setenv("https_proxy", new_proxy_url)

        proxy_setup.ensure_proxy_running(hook_settings, client)

        assert "newuser" in creds_file.read_text(), "Creds file should have new credentials"
        assert client.is_service_running(BAZEL_PROXY_SERVICE), "Proxy should still be running"


if __name__ == "__main__":
    pytest_bazel.main()
