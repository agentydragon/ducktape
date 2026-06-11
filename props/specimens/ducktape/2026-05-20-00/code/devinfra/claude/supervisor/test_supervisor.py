"""Integration tests for supervisor management.

Tests supervisor client functionality (lifecycle, add/update/check services)
without requiring the full proxy infrastructure.
"""

import pytest_bazel

from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings
from devinfra.claude.supervisor.setup import start as supervisor_start
from devinfra.claude.testing.fixtures import IsolatedSupervisorDirs, supervisor_is_running

# Register shared fixtures (isolated_dirs, session_paths, hook_settings)
pytest_plugins = ["devinfra.claude.testing.fixtures"]


async def test_supervisor_lifecycle(
    isolated_dirs: IsolatedSupervisorDirs, session_paths: SessionPaths, hook_settings: HookSettings
) -> None:
    """Test supervisor start/stop lifecycle."""
    assert not await supervisor_is_running(session_paths, hook_settings)

    await supervisor_start(session_paths, hook_settings)
    assert await supervisor_is_running(session_paths, hook_settings)

    # Start again should be idempotent
    await supervisor_start(session_paths, hook_settings)
    assert await supervisor_is_running(session_paths, hook_settings)


async def test_add_and_check_service(
    isolated_dirs: IsolatedSupervisorDirs, session_paths: SessionPaths, hook_settings: HookSettings
) -> None:
    """Test adding a service to supervisor."""
    supervisor_result = await supervisor_start(session_paths, hook_settings)

    await supervisor_result.add_service(
        name="test-service", command="sleep 3600", directory=isolated_dirs.supervisor_dir
    )

    await supervisor_result.wait_for_service_running("test-service")


async def test_update_service(
    isolated_dirs: IsolatedSupervisorDirs, session_paths: SessionPaths, hook_settings: HookSettings
) -> None:
    """Test updating a service config."""
    supervisor_result = await supervisor_start(session_paths, hook_settings)

    await supervisor_result.add_service(
        name="test-service", command="sleep 3600", directory=isolated_dirs.supervisor_dir
    )

    initial_info = await supervisor_result.get_process_info("test-service")
    initial_pid = initial_info.pid

    await supervisor_result.update_service(
        name="test-service", command="sleep 7200", directory=isolated_dirs.supervisor_dir
    )

    # Verify restarted (PID should have changed)
    new_info = await supervisor_result.get_process_info("test-service")
    assert new_info.pid != initial_pid, f"Service should have been restarted (PID unchanged: {initial_pid})"


if __name__ == "__main__":
    pytest_bazel.main()
