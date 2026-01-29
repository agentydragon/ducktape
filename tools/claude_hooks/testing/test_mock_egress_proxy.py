"""Tests for MockEgressProxy test utility."""

from __future__ import annotations

import base64
import socket
from collections.abc import Generator

import pytest
import pytest_bazel

from tools.claude_hooks.testing.fixtures import MockEgressProxyFixture
from tools.claude_hooks.testing.mock_egress_proxy import MockEgressProxy

# Register fixtures from module (pytest-native, no direct name import needed)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


@pytest.fixture
def proxy_socket(mock_egress_proxy: MockEgressProxyFixture) -> Generator[socket.socket]:
    """Socket connected to the mock proxy."""
    sock = socket.create_connection(("127.0.0.1", mock_egress_proxy.proxy.port), timeout=5)
    try:
        yield sock
    finally:
        sock.close()


def test_proxy_starts_and_stops() -> None:
    """Test basic proxy lifecycle via context manager."""
    with MockEgressProxy(upstream_proxy=None, listen_port=0, require_auth=False) as proxy:
        assert proxy.port > 0
        assert proxy.ca_cert_pem


def test_proxy_requires_auth(proxy_socket: socket.socket) -> None:
    """Test that proxy rejects unauthenticated requests."""
    proxy_socket.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    response = proxy_socket.recv(1024)
    assert b"407" in response, f"Expected 407, got: {response!r}"


def test_proxy_accepts_auth(proxy_socket: socket.socket) -> None:
    """Test that proxy accepts valid authentication."""
    creds = base64.b64encode(b"proxy_user:test_jwt_token").decode()
    request = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {creds}\r\n\r\n"
    proxy_socket.sendall(request.encode())
    response = proxy_socket.recv(1024)
    assert b"200" in response, f"Expected 200, got: {response!r}"


if __name__ == "__main__":
    pytest_bazel.main()
