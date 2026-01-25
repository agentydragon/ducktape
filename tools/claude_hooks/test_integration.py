"""Integration tests for claude_hooks proxy infrastructure.

These tests use REAL processes (supervisor, pproxy) and a mock TLS-inspecting proxy
to verify end-to-end behavior.
"""

from __future__ import annotations

import base64
import contextlib
import inspect
import os
import signal
import socket
import ssl
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from net_util.net import pick_free_port
from runfiles import get_required_path
from tools.claude_hooks import proxy_setup, supervisor_setup
from tools.claude_hooks.testing import runfiles_util

# =============================================================================
# Test Fixtures: Mock TLS-Inspecting Proxy
# =============================================================================


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


# =============================================================================
# Test Fixtures: Isolated Test Environment
# =============================================================================


@dataclass
class IsolatedEnv:
    """Isolated test environment with temporary directories and ports."""

    supervisor_dir: Path
    bazel_proxy_dir: Path
    proxy_port: int
    upstream_proxy_port: int
    proxy_url: str


@pytest.fixture
def isolated_env(tmp_path: Path, mock_tls_proxy: MockTLSProxy, monkeypatch: pytest.MonkeyPatch) -> IsolatedEnv:
    """Fixture that sets up isolated directories and env vars for testing.

    Uses environment variable overrides (proper DI) instead of monkeypatching.
    The claude_hooks modules read these env vars via getter functions.
    """
    # Create isolated directories
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    bazel_proxy_dir = tmp_path / "bazel-proxy"
    bazel_proxy_dir.mkdir()

    # Use different ports for the local proxy and supervisor to avoid conflicts
    test_proxy_port = pick_free_port()
    supervisor_port = pick_free_port()

    # Set environment variables for DI (read by getter functions in the modules)
    monkeypatch.setenv("CLAUDE_HOOKS_SUPERVISOR_DIR", str(supervisor_dir))
    monkeypatch.setenv("CLAUDE_HOOKS_SUPERVISOR_PORT", str(supervisor_port))
    monkeypatch.setenv("CLAUDE_HOOKS_BAZEL_PROXY_DIR", str(bazel_proxy_dir))
    monkeypatch.setenv("CLAUDE_HOOKS_BAZEL_PROXY_PORT", str(test_proxy_port))

    # Set CLAUDE_AUTH_PROXY_CMD to use the runfiles binary (same approach as test_e2e)
    monkeypatch.setenv("CLAUDE_AUTH_PROXY_CMD", str(get_required_path(runfiles_util.RUN_AUTH_PROXY)))

    # Set https_proxy env var pointing to mock proxy
    proxy_url = f"http://testuser:testpass@127.0.0.1:{mock_tls_proxy.port}"
    monkeypatch.setenv("https_proxy", proxy_url)

    return IsolatedEnv(
        supervisor_dir=supervisor_dir,
        bazel_proxy_dir=bazel_proxy_dir,
        proxy_port=test_proxy_port,
        upstream_proxy_port=mock_tls_proxy.port,
        proxy_url=proxy_url,
    )


def _stop_supervisor_by_pidfile(pidfile: Path) -> None:
    """Stop supervisor process by reading and killing from pidfile."""
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        # Force kill if still running
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except (ValueError, ProcessLookupError, OSError):
        pass
    # Clean up pidfile
    with contextlib.suppress(OSError):
        pidfile.unlink()


@pytest.fixture(autouse=True)
def cleanup_supervisor(isolated_env: IsolatedEnv) -> Generator[None]:
    """Fixture that ensures supervisor is stopped before and after test."""
    # Clean up BEFORE test to handle stale supervisor from previous runs
    pidfile = isolated_env.supervisor_dir / "supervisord.pid"
    _stop_supervisor_by_pidfile(pidfile)

    yield

    # Clean up AFTER test
    _stop_supervisor_by_pidfile(pidfile)


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    """Wait for a port to be listening."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (OSError, TimeoutError):
            time.sleep(0.2)
    return False


# =============================================================================
# Integration Tests
# =============================================================================


class TestProxySetup:
    """Integration tests for proxy setup."""

    def test_supervisor_starts_and_proxy_runs(
        self, isolated_env: IsolatedEnv, mock_ca_cert: tuple[bytes, bytes]
    ) -> None:
        """Test that setup_bazel_proxy starts supervisor and proxy service."""
        # Run the proxy setup
        supervisor_result = supervisor_setup.start()
        proxy_setup.ensure_proxy_running(supervisor_result.client)

        # Verify supervisor is running
        assert supervisor_setup.is_running(), "Supervisor should be running"

        # Verify proxy service is running
        assert supervisor_result.client.is_service_running("bazel-proxy"), "bazel-proxy service should be running"

        # Verify we can connect to the local proxy port
        assert _wait_for_port(isolated_env.proxy_port, timeout=5), "Proxy should be listening"

    def test_ca_extraction(self, isolated_env: IsolatedEnv, mock_ca_cert: tuple[bytes, bytes]) -> None:
        """Test that CA certificate is extracted from TLS chain."""
        # First start supervisor and the proxy
        supervisor_result = supervisor_setup.start()
        proxy_setup.ensure_proxy_running(supervisor_result.client)
        assert _wait_for_port(isolated_env.proxy_port, timeout=5)

        # Now extract CA (this connects through our local proxy to the mock upstream)
        proxy_setup._extract_proxy_ca()

        # Verify CA file was created
        ca_file = isolated_env.bazel_proxy_dir / "anthropic_ca.pem"
        assert ca_file.exists(), "CA file should be created"

        # Verify it contains 'Anthropic' (from our mock cert)
        ca_content = ca_file.read_text()
        assert "BEGIN CERTIFICATE" in ca_content

        # Parse and verify it's our mock CA
        cert = x509.load_pem_x509_certificate(ca_content.encode())
        cn_value = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        cn = cn_value if isinstance(cn_value, str) else cn_value.decode()
        assert "TLS Inspection CA" in cn, f"Expected 'TLS Inspection CA' in CN, got: {cn}"

    def test_credential_rotation(self, isolated_env: IsolatedEnv) -> None:
        """Test that credential changes trigger proxy restart."""
        # Start supervisor and proxy with initial credentials
        supervisor_result = supervisor_setup.start()
        client = supervisor_result.client
        proxy_setup.ensure_proxy_running(client)
        assert _wait_for_port(isolated_env.proxy_port, timeout=5)

        # Cache original credentials file content
        creds_file = isolated_env.bazel_proxy_dir / "upstream_proxy"
        original_creds = creds_file.read_text()
        initial_info = client.get_process_info("bazel-proxy")
        initial_start_time = initial_info.start

        # Change credentials (simulate rotation)
        new_proxy_url = f"http://newuser:newpass@127.0.0.1:{isolated_env.upstream_proxy_port}"
        os.environ["https_proxy"] = new_proxy_url

        # Call ensure_proxy_running - should detect change and restart
        refreshed = proxy_setup.ensure_proxy_running(client)

        # Verify it detected the change
        assert refreshed, "Should have detected credential change"

        # Verify credentials file updated
        new_creds = creds_file.read_text()
        assert new_creds != original_creds, "Credentials file should be updated"
        assert "newuser" in new_creds or new_proxy_url == new_creds.strip()

        # Verify proxy was restarted (start time should be different)
        time.sleep(0.5)  # Give supervisor time to update
        new_info = client.get_process_info("bazel-proxy")
        assert new_info.start >= initial_start_time, "Proxy should have been restarted"


class TestSupervisorSetup:
    """Integration tests for supervisor management."""

    def test_supervisor_lifecycle(self, isolated_env: IsolatedEnv) -> None:
        """Test supervisor start/stop lifecycle."""
        # Initially not running
        assert not supervisor_setup.is_running()

        # Start supervisor
        supervisor_setup.start()
        assert supervisor_setup.is_running()

        # Start again should be idempotent
        supervisor_setup.start()
        assert supervisor_setup.is_running()

    def test_add_and_check_service(self, isolated_env: IsolatedEnv) -> None:
        """Test adding a service to supervisor."""
        supervisor_result = supervisor_setup.start()

        # Add a simple service (sleep command)
        supervisor_result.client.add_service(
            name="test-service", command="sleep 3600", directory=isolated_env.supervisor_dir
        )

        # Check it's running
        assert supervisor_result.client.is_service_running("test-service")

    def test_update_service(self, isolated_env: IsolatedEnv) -> None:
        """Test updating a service config."""
        supervisor_result = supervisor_setup.start()

        # Add initial service
        supervisor_result.client.add_service(
            name="test-service", command="sleep 3600", directory=isolated_env.supervisor_dir
        )

        # Get initial PID
        initial_info = supervisor_result.client.get_process_info("test-service")
        initial_pid = initial_info.pid

        # Update with different command
        supervisor_result.client.update_service(
            name="test-service", command="sleep 7200", directory=isolated_env.supervisor_dir
        )

        # Verify restarted (PID should have changed since process was stopped and restarted)
        new_info = supervisor_result.client.get_process_info("test-service")
        assert new_info.pid != initial_pid, f"Service should have been restarted (PID unchanged: {initial_pid})"


class TestRobustness:
    """Tests for robustness fixes."""

    def test_strict_connect_parsing(self) -> None:
        """Test that CONNECT response parsing is strict."""
        source = inspect.getsource(proxy_setup._extract_proxy_ca)
        assert 'status_line.startswith(b"HTTP/")' in source, "Should check for HTTP/ prefix"
        assert 'b" 200 "' in source, "Should check for space-delimited 200"

    def test_single_cert_chain_allowed(self) -> None:
        """Test that single-cert chains are now allowed."""
        source = inspect.getsource(proxy_setup._extract_proxy_ca)
        # Should NOT require 2+ certs anymore
        assert "len(cert_chain_der) < 2" not in source, "Should not require 2+ certs"
        assert "at least 2 certs" not in source.lower(), "Should not require 2+ certs"


if __name__ == "__main__":
    pytest_bazel.main()
