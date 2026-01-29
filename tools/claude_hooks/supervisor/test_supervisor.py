"""Integration tests for supervisor management.

Tests supervisor client functionality (lifecycle, add/update/check services)
without requiring the full proxy infrastructure.
"""

from __future__ import annotations

import pytest_bazel

from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.setup import start as supervisor_start
from tools.claude_hooks.testing.fixtures import IsolatedSupervisorDirs, supervisor_is_running

# Register shared fixtures (isolated_dirs, hook_settings)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]


async def test_supervisor_lifecycle(isolated_dirs: IsolatedSupervisorDirs, hook_settings: HookSettings) -> None:
    """Test supervisor start/stop lifecycle."""
    assert not await supervisor_is_running(hook_settings)

    await supervisor_start(hook_settings)
    assert await supervisor_is_running(hook_settings)

    # Start again should be idempotent
    await supervisor_start(hook_settings)
    assert await supervisor_is_running(hook_settings)


async def test_add_and_check_service(isolated_dirs: IsolatedSupervisorDirs, hook_settings: HookSettings) -> None:
    """Test adding a service to supervisor."""
    supervisor_result = await supervisor_start(hook_settings)

    await supervisor_result.client.add_service(
        name="test-service", command="sleep 3600", directory=isolated_dirs.supervisor_dir
    )

    await supervisor_result.client.wait_for_service_running("test-service")


async def test_update_service(isolated_dirs: IsolatedSupervisorDirs, hook_settings: HookSettings) -> None:
    """Test updating a service config."""
    supervisor_result = await supervisor_start(hook_settings)

    await supervisor_result.client.add_service(
        name="test-service", command="sleep 3600", directory=isolated_dirs.supervisor_dir
    )

    initial_info = await supervisor_result.client.get_process_info("test-service")
    initial_pid = initial_info.pid

    await supervisor_result.client.update_service(
        name="test-service", command="sleep 7200", directory=isolated_dirs.supervisor_dir
    )

    # Verify restarted (PID should have changed)
    new_info = await supervisor_result.client.get_process_info("test-service")
    assert new_info.pid != initial_pid, f"Service should have been restarted (PID unchanged: {initial_pid})"


if __name__ == "__main__":
    pytest_bazel.main()
