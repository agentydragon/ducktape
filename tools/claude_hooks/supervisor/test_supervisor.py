"""Integration tests for supervisor management.

Tests supervisor client functionality (lifecycle, add/update/check services)
without requiring the full proxy infrastructure.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel

from net_util.net import pick_free_port
from tools.claude_hooks import settings
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import is_running as supervisor_is_running
from tools.claude_hooks.supervisor.setup import start as supervisor_start
from tools.claude_hooks.testing.supervisor_cleanup import supervisor_cleanup


@pytest.fixture
def isolated_supervisor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture that sets up isolated directories for supervisor testing.

    Returns the supervisor directory path.
    """
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    bazel_proxy_dir = tmp_path / "bazel-proxy"
    bazel_proxy_dir.mkdir()

    supervisor_port = pick_free_port()

    monkeypatch.setenv(settings.ENV_SUPERVISOR_DIR, str(supervisor_dir))
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(supervisor_port))
    monkeypatch.setenv(settings.ENV_BAZEL_PROXY_DIR, str(bazel_proxy_dir))

    return supervisor_dir


@pytest.fixture
def hook_settings(isolated_supervisor_env: Path) -> HookSettings:
    """Fixture that creates HookSettings after env vars are configured."""
    return HookSettings()


@pytest.fixture(autouse=True)
def cleanup_supervisor_fixture(isolated_supervisor_env: Path) -> Generator[None]:
    """Fixture that ensures supervisor is stopped before and after test."""
    with supervisor_cleanup(isolated_supervisor_env / "supervisord.pid"):
        yield


class TestSupervisorLifecycle:
    """Tests for supervisor start/stop lifecycle."""

    def test_supervisor_lifecycle(self, isolated_supervisor_env: Path, hook_settings: HookSettings) -> None:
        """Test supervisor start/stop lifecycle."""
        assert not supervisor_is_running(hook_settings)

        supervisor_start(hook_settings)
        assert supervisor_is_running(hook_settings)

        # Start again should be idempotent
        supervisor_start(hook_settings)
        assert supervisor_is_running(hook_settings)


class TestSupervisorServices:
    """Tests for supervisor service management."""

    def test_add_and_check_service(self, isolated_supervisor_env: Path, hook_settings: HookSettings) -> None:
        """Test adding a service to supervisor."""
        supervisor_result = supervisor_start(hook_settings)

        supervisor_result.client.add_service(
            name="test-service", command="sleep 3600", directory=isolated_supervisor_env
        )

        assert supervisor_result.client.is_service_running("test-service")

    def test_update_service(self, isolated_supervisor_env: Path, hook_settings: HookSettings) -> None:
        """Test updating a service config."""
        supervisor_result = supervisor_start(hook_settings)

        supervisor_result.client.add_service(
            name="test-service", command="sleep 3600", directory=isolated_supervisor_env
        )

        initial_info = supervisor_result.client.get_process_info("test-service")
        initial_pid = initial_info.pid

        supervisor_result.client.update_service(
            name="test-service", command="sleep 7200", directory=isolated_supervisor_env
        )

        # Verify restarted (PID should have changed)
        new_info = supervisor_result.client.get_process_info("test-service")
        assert new_info.pid != initial_pid, f"Service should have been restarted (PID unchanged: {initial_pid})"


if __name__ == "__main__":
    pytest_bazel.main()
