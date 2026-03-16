"""Unit tests for bazel_wrapper supervisor restart logic."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel

from devinfra.claude.bazel_wrapper import _ensure_proxy_with_supervisor_restart
from devinfra.claude.errors import SupervisorError
from devinfra.claude.settings import HookSettings


async def test_supervisor_reachable_skips_restart(hook_settings: HookSettings) -> None:
    """When supervisor is reachable, proxy setup runs without restart."""
    with (
        patch(
            "devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=AsyncMock()
        ) as mock_try_connect,
        patch("devinfra.claude.bazel_wrapper.proxy_setup.ensure_proxy_running", new_callable=AsyncMock) as mock_ensure,
        patch("devinfra.claude.bazel_wrapper.supervisor_start", new_callable=AsyncMock) as mock_start,
    ):
        await _ensure_proxy_with_supervisor_restart(hook_settings)

        mock_try_connect.assert_awaited_once_with(hook_settings)
        mock_ensure.assert_awaited_once()
        mock_start.assert_not_awaited()


async def test_supervisor_dead_triggers_restart(hook_settings: HookSettings) -> None:
    """When supervisor is unreachable, it gets restarted before proxy setup."""
    with (
        patch(
            "devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=None
        ) as mock_try_connect,
        patch("devinfra.claude.bazel_wrapper.proxy_setup.ensure_proxy_running", new_callable=AsyncMock) as mock_ensure,
        patch("devinfra.claude.bazel_wrapper.supervisor_start", new_callable=AsyncMock) as mock_start,
    ):
        await _ensure_proxy_with_supervisor_restart(hook_settings)

        mock_try_connect.assert_awaited_once_with(hook_settings)
        mock_start.assert_awaited_once_with(hook_settings)
        mock_ensure.assert_awaited_once()


async def test_supervisor_restart_failure_propagates(hook_settings: HookSettings) -> None:
    """When supervisor restart fails, the error propagates as SupervisorError."""
    with (
        patch("devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=None),
        patch(
            "devinfra.claude.bazel_wrapper.supervisor_start",
            new_callable=AsyncMock,
            side_effect=SupervisorError("supervisord did not start in time"),
        ),
        pytest.raises(SupervisorError, match="did not start in time"),
    ):
        await _ensure_proxy_with_supervisor_restart(hook_settings)


if __name__ == "__main__":
    pytest_bazel.main()
