"""Integration tests for claude_hooks proxy infrastructure.

These tests use REAL processes (supervisor, auth_forwarding_proxy) and a TLS-inspecting proxy
to verify end-to-end behavior.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel
from cryptography import x509
from cryptography.x509.oid import NameOID

from net_util.net import pick_free_port, wait_for_port
from tools.claude_hooks import proxy_setup, settings
from tools.claude_hooks.proxy_setup import BAZEL_PROXY_SERVICE
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import is_running as supervisor_is_running
from tools.claude_hooks.supervisor.setup import start as supervisor_start
from tools.claude_hooks.testing.fixtures import MockProxyFixture
from tools.claude_hooks.testing.supervisor_cleanup import supervisor_cleanup

# Register fixtures from module (pytest-native, no direct name import needed)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


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

    return supervisor_dir, bazel_proxy_dir


@pytest.fixture
def hook_settings(
    isolated_dirs: tuple[Path, Path], mock_anthropic_proxy: MockProxyFixture, monkeypatch: pytest.MonkeyPatch
) -> HookSettings:
    """Fixture that creates HookSettings with upstream proxy configured."""
    monkeypatch.setenv("https_proxy", mock_anthropic_proxy.proxy.url)
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
        self, hook_settings: HookSettings, mock_anthropic_proxy: MockProxyFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that credential changes are written to file (hot-reload)."""
        supervisor_result = supervisor_start(hook_settings)
        client = supervisor_result.client
        proxy_setup.ensure_proxy_running(hook_settings, client)
        wait_for_port("127.0.0.1", hook_settings.get_bazel_proxy_port(), timeout_secs=5)

        creds_file = hook_settings.get_bazel_creds_file()
        assert creds_file.exists(), "Creds file should exist"
        assert "proxy_user" in creds_file.read_text(), "Initial creds should have original credentials"

        new_proxy_url = f"http://newuser:newpass@127.0.0.1:{mock_anthropic_proxy.proxy.port}"
        monkeypatch.setenv("https_proxy", new_proxy_url)

        proxy_setup.ensure_proxy_running(hook_settings, client)

        assert "newuser" in creds_file.read_text(), "Creds file should have new credentials"
        assert client.is_service_running(BAZEL_PROXY_SERVICE), "Proxy should still be running"


if __name__ == "__main__":
    pytest_bazel.main()
