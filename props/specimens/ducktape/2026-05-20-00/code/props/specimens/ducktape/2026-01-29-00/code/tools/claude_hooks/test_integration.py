"""Integration tests for claude_hooks proxy infrastructure.

These tests use REAL processes (supervisor, auth proxy) and a TLS-inspecting proxy
to verify end-to-end behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.x509.oid import NameOID

from net_util.net import async_wait_for_port
from tools.claude_hooks import proxy_setup
from tools.claude_hooks.proxy_setup import AUTH_PROXY_SERVICE
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.setup import start as supervisor_start
from tools.claude_hooks.testing.fixtures import MockEgressProxyFixture, supervisor_is_running

# Register shared fixtures (isolated_dirs, hook_settings, mock_egress_proxy)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


@pytest.fixture
def hook_settings(
    isolated_dirs, mock_egress_proxy: MockEgressProxyFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> HookSettings:
    """Override shared hook_settings to also configure upstream proxy and CA path."""
    monkeypatch.setenv("https_proxy", mock_egress_proxy.proxy.url)
    # Write mock CA to a temp file so _extract_proxy_ca can load it from filesystem
    ca_file = tmp_path / "mock-ca.crt"
    ca_file.write_bytes(mock_egress_proxy.proxy.ca_cert_pem)
    monkeypatch.setenv("ANTHROPIC_CA_PATH", str(ca_file))
    return HookSettings()


async def test_supervisor_starts_and_proxy_runs(hook_settings: HookSettings) -> None:
    """Test that setup_auth_proxy starts supervisor and proxy service."""
    supervisor_result = await supervisor_start(hook_settings)
    await proxy_setup.ensure_proxy_running(hook_settings, supervisor_result.client)

    assert await supervisor_is_running(hook_settings), "Supervisor should be running"
    assert await supervisor_result.client.is_service_running(AUTH_PROXY_SERVICE), "auth-proxy service should be running"
    await async_wait_for_port("127.0.0.1", hook_settings.get_auth_proxy_port(), timeout_secs=5)


async def test_ca_extraction(hook_settings: HookSettings) -> None:
    """Test that CA certificate is extracted from TLS chain."""
    supervisor_result = await supervisor_start(hook_settings)
    await proxy_setup.ensure_proxy_running(hook_settings, supervisor_result.client)
    await async_wait_for_port("127.0.0.1", hook_settings.get_auth_proxy_port(), timeout_secs=5)

    proxy_setup._extract_proxy_ca(hook_settings)

    ca_file = hook_settings.get_auth_proxy_ca_file()
    assert ca_file.exists(), "CA file should be created"

    ca_content = ca_file.read_text()
    assert "BEGIN CERTIFICATE" in ca_content

    cert = x509.load_pem_x509_certificate(ca_content.encode())
    cn_value = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    cn = cn_value if isinstance(cn_value, str) else cn_value.decode()
    assert "TLS Inspection CA" in cn, f"Expected 'TLS Inspection CA' in CN, got: {cn}"


async def test_credential_rotation(
    hook_settings: HookSettings, mock_egress_proxy: MockEgressProxyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that credential changes are written to file (hot-reload)."""
    supervisor_result = await supervisor_start(hook_settings)
    client = supervisor_result.client
    await proxy_setup.ensure_proxy_running(hook_settings, client)
    await async_wait_for_port("127.0.0.1", hook_settings.get_auth_proxy_port(), timeout_secs=5)

    creds_file = hook_settings.get_auth_proxy_creds_file()
    assert creds_file.exists(), "Creds file should exist"
    assert "proxy_user" in creds_file.read_text(), "Initial creds should have original credentials"

    new_proxy_url = f"http://newuser:newpass@127.0.0.1:{mock_egress_proxy.proxy.port}"
    monkeypatch.setenv("https_proxy", new_proxy_url)

    await proxy_setup.ensure_proxy_running(hook_settings, client)

    assert "newuser" in creds_file.read_text(), "Creds file should have new credentials"
    assert await client.is_service_running(AUTH_PROXY_SERVICE), "Proxy should still be running"


if __name__ == "__main__":
    pytest_bazel.main()
