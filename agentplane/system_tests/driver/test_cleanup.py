"""Cancellation-safe cleanup completion behavior."""

import asyncio

import pytest_bazel

from agentplane.system_tests.driver.cleanup import finish_cleanup


async def test_finish_cleanup_reports_child_cancellation_as_a_failure() -> None:
    async def cleanup() -> None:
        raise asyncio.CancelledError("cleanup task stopped")

    result = await finish_cleanup(cleanup())

    assert isinstance(result.failure, asyncio.CancelledError)
    assert result.cancellation is None


if __name__ == "__main__":
    pytest_bazel.main()
