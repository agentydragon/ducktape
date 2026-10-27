"""A cleanup error must not replace the scenario failure it followed."""

import sys

import pytest
import pytest_bazel

from agentplane.system_tests.cleanup import cleanup_preserving_failure


async def test_cleanup_failure_keeps_scenario_failure_and_traceback() -> None:
    scenario_failure = ValueError("scenario failed")
    cleanup_failure = RuntimeError("Sandbox did not stop")

    async def cleanup() -> None:
        raise cleanup_failure

    async def scenario() -> None:
        try:
            raise scenario_failure
        finally:
            await cleanup_preserving_failure(cleanup, sys.exception())

    with pytest.raises(ValueError, match="scenario failed") as raised:
        await scenario()
    assert raised.value is scenario_failure
    assert "RuntimeError: Sandbox did not stop" in raised.value.__notes__[0]
    assert "in cleanup" in raised.value.__notes__[0]


async def test_cleanup_failure_fails_a_successful_scenario() -> None:
    cleanup_failure = RuntimeError("Sandbox did not stop")

    async def cleanup() -> None:
        raise cleanup_failure

    with pytest.raises(RuntimeError) as raised:
        await cleanup_preserving_failure(cleanup, None)
    assert raised.value is cleanup_failure


async def test_cleanup_success_does_not_change_scenario_result() -> None:
    cleaned = False

    async def cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    await cleanup_preserving_failure(cleanup, None)
    assert cleaned


if __name__ == "__main__":
    pytest_bazel.main()
