"""Unit tests for bazel_wrapper proxy credential refresh logic."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel

from devinfra.claude.bazel_wrapper import _ensure_proxy_creds_fresh
from devinfra.claude.errors import AuthProxyError
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings


async def test_writes_creds_and_verifies_port(
    session_paths: SessionPaths, hook_settings: HookSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When proxy is listening, creds are written and no error is raised."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy.example.com:8080")

    with patch("devinfra.claude.bazel_wrapper.async_wait_for_port", new_callable=AsyncMock) as mock_wait:
        await _ensure_proxy_creds_fresh(session_paths, hook_settings)

        mock_wait.assert_awaited_once_with("127.0.0.1", hook_settings.auth_proxy_port, timeout_secs=5.0)

    # Verify creds were written to canonical location
    creds_file = session_paths.auth_proxy_creds_file
    assert creds_file.exists()
    assert "user:pass" in creds_file.read_text()


async def test_raises_when_no_proxy_env(
    session_paths: SessionPaths, hook_settings: HookSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When HTTPS_PROXY is not set, raises AuthProxyError."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    with pytest.raises(AuthProxyError, match="No HTTPS_PROXY"):
        await _ensure_proxy_creds_fresh(session_paths, hook_settings)


async def test_raises_when_proxy_not_listening(
    session_paths: SessionPaths, hook_settings: HookSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When proxy port is not listening, raises AuthProxyError with restart guidance."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy.example.com:8080")

    with (
        patch(
            "devinfra.claude.bazel_wrapper.async_wait_for_port",
            new_callable=AsyncMock,
            side_effect=TimeoutError("port not ready"),
        ),
        pytest.raises(AuthProxyError, match="not listening"),
    ):
        await _ensure_proxy_creds_fresh(session_paths, hook_settings)


if __name__ == "__main__":
    pytest_bazel.main()
