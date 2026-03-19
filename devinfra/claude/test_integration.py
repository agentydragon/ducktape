"""Integration tests for claude proxy infrastructure.

These tests use the in-process auth proxy and a TLS-inspecting mock proxy
to verify end-to-end behavior.
"""

from pathlib import Path

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.x509.oid import NameOID

from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.auth_proxy.proxy import AuthForwardingProxy
from devinfra.claude.auth_proxy.vars import get_upstream_proxy_url
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings
from devinfra.claude.testing.fixtures import MockEgressProxyFixture
from util.net import async_wait_for_port

# Register shared fixtures (isolated_dirs, session_paths, hook_settings, mock_egress_proxy)
pytest_plugins = ["devinfra.claude.testing.fixtures"]


@pytest.fixture
def hook_settings(
    isolated_dirs, mock_egress_proxy: MockEgressProxyFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> HookSettings:
    """Override shared hook_settings to also configure upstream proxy and CA path."""
    # Set HTTPS_PROXY (uppercase) which get_upstream_proxy_url() checks first.
    # Also clear lowercase to avoid ambiguity.
    monkeypatch.setenv("HTTPS_PROXY", mock_egress_proxy.proxy.url)
    monkeypatch.delenv("https_proxy", raising=False)
    # Write mock CA to a temp file so _extract_proxy_ca can load it from filesystem
    ca_file = tmp_path / "mock-ca.crt"
    ca_file.write_bytes(mock_egress_proxy.proxy.ca_cert_pem)
    monkeypatch.setenv("ANTHROPIC_CA_PATH", str(ca_file))
    return HookSettings()


@pytest.fixture
async def auth_proxy(session_paths: SessionPaths, hook_settings: HookSettings):
    """Start an in-process auth proxy and clean up after test."""
    creds_file = session_paths.auth_proxy_creds_file
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    # Write initial creds from upstream proxy URL
    https_proxy = get_upstream_proxy_url()
    assert https_proxy, "HTTPS_PROXY must be set"
    creds_file.write_text(https_proxy)

    proxy = AuthForwardingProxy(listen_port=hook_settings.auth_proxy_port, creds_file=creds_file)
    proxy.start()
    await async_wait_for_port("127.0.0.1", hook_settings.auth_proxy_port, timeout_secs=5)
    try:
        yield proxy
    finally:
        proxy.stop()


async def test_proxy_starts_and_listens(auth_proxy: AuthForwardingProxy, hook_settings: HookSettings) -> None:
    """Test that the in-process auth proxy starts and listens on the configured port."""
    assert auth_proxy._running, "Auth proxy should be running"
    await async_wait_for_port("127.0.0.1", hook_settings.auth_proxy_port, timeout_secs=5)


async def test_ca_extraction(
    auth_proxy: AuthForwardingProxy, session_paths: SessionPaths, hook_settings: HookSettings
) -> None:
    """Test that CA certificate is extracted from the filesystem."""
    proxy_setup._extract_proxy_ca(session_paths)

    ca_file = session_paths.auth_proxy_ca_file
    assert ca_file.exists(), "CA file should be created"

    ca_content = ca_file.read_text()
    assert "BEGIN CERTIFICATE" in ca_content

    cert = x509.load_pem_x509_certificate(ca_content.encode())
    cn_value = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    cn = cn_value if isinstance(cn_value, str) else cn_value.decode()
    assert "TLS Inspection CA" in cn, f"Expected 'TLS Inspection CA' in CN, got: {cn}"


async def test_credential_rotation(
    auth_proxy: AuthForwardingProxy,
    session_paths: SessionPaths,
    hook_settings: HookSettings,
    mock_egress_proxy: MockEgressProxyFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that credential changes are written to the proxy's creds file (hot-reload)."""
    creds_file = auth_proxy.creds_file
    assert creds_file.exists(), "Creds file should exist"
    assert "proxy_user" in creds_file.read_text(), "Initial creds should have original credentials"

    # Simulate credential rotation
    new_proxy_url = f"http://newuser:newpass@127.0.0.1:{mock_egress_proxy.proxy.port}"
    monkeypatch.setenv("HTTPS_PROXY", new_proxy_url)

    # Re-run setup — should update creds file
    result = await proxy_setup.setup_auth_proxy(session_paths, hook_settings, proxy=auth_proxy)

    assert "newuser" in creds_file.read_text(), "Creds file should have new credentials"
    assert auth_proxy._running, "Proxy should still be running"
    assert result.status.startswith("running"), f"Expected running status, got: {result.status}"


if __name__ == "__main__":
    pytest_bazel.main()
