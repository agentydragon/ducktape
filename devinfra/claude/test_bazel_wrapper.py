"""Unit tests for bazel_wrapper supervisor restart logic."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel

from devinfra.claude.bazel_wrapper import _ensure_proxy_with_supervisor_restart
from devinfra.claude.errors import SupervisorError
from devinfra.claude.settings import HookSettings
from devinfra.claude.supervisor.client import SupervisorClient
from devinfra.claude.supervisor.setup import SupervisorSetup


async def test_supervisor_reachable_skips_restart(tmp_path: object) -> None:
    """When supervisor is reachable, proxy setup runs without restart."""
    settings = HookSettings()
    mock_client = AsyncMock(spec=SupervisorClient)

    with (
        patch(
            "devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=mock_client
        ) as mock_try_connect,
        patch("devinfra.claude.bazel_wrapper.proxy_setup.ensure_proxy_running", new_callable=AsyncMock) as mock_ensure,
        patch("devinfra.claude.bazel_wrapper.supervisor_start", new_callable=AsyncMock) as mock_start,
    ):
        await _ensure_proxy_with_supervisor_restart(settings)

        mock_try_connect.assert_awaited_once_with(settings)
        mock_ensure.assert_awaited_once()
        mock_start.assert_not_awaited()


async def test_supervisor_dead_triggers_restart(tmp_path: object) -> None:
    """When supervisor is unreachable, it gets restarted before proxy setup."""
    settings = HookSettings()
    mock_client = AsyncMock(spec=SupervisorClient)
    mock_setup_result = SupervisorSetup(client=mock_client, settings=settings)

    with (
        patch(
            "devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=None
        ) as mock_try_connect,
        patch("devinfra.claude.bazel_wrapper.proxy_setup.ensure_proxy_running", new_callable=AsyncMock) as mock_ensure,
        patch(
            "devinfra.claude.bazel_wrapper.supervisor_start", new_callable=AsyncMock, return_value=mock_setup_result
        ) as mock_start,
    ):
        await _ensure_proxy_with_supervisor_restart(settings)

        mock_try_connect.assert_awaited_once_with(settings)
        mock_start.assert_awaited_once_with(settings)
        # ensure_proxy_running should be called with the new client from restart
        mock_ensure.assert_awaited_once()
        call_args = mock_ensure.call_args
        assert call_args[0][1] is mock_client


async def test_supervisor_restart_failure_propagates() -> None:
    """When supervisor restart fails, the error propagates as SupervisorError."""
    settings = HookSettings()

    with (
        patch("devinfra.claude.bazel_wrapper.try_connect", new_callable=AsyncMock, return_value=None),
        patch(
            "devinfra.claude.bazel_wrapper.supervisor_start",
            new_callable=AsyncMock,
            side_effect=SupervisorError("supervisord did not start in time"),
        ),
        pytest.raises(SupervisorError, match="did not start in time"),
    ):
        await _ensure_proxy_with_supervisor_restart(settings)


if __name__ == "__main__":
    pytest_bazel.main()
