import asyncio

import pytest

from agentplane.system_tests.driver.cleanup_diagnostics import capture_without_masking, cleanup_without_masking


def test_diagnostic_failure_keeps_original_cleanup_error_without_capture_text() -> None:
    async def run() -> None:
        cleanup_error = RuntimeError("cleanup wait failed")

        async def capture() -> None:
            raise RuntimeError("PRIVATE_PROMPT_AND_CREDENTIAL")

        async def trigger_cleanup_with_scenario_failure() -> None:
            try:
                raise cleanup_error
            except Exception as error:
                await capture_without_masking(error, capture)
                raise

        with pytest.raises(RuntimeError, match="cleanup wait failed") as raised:
            await trigger_cleanup_with_scenario_failure()

        assert raised.value is cleanup_error
        assert "PRIVATE_PROMPT_AND_CREDENTIAL" not in "\n".join(raised.value.__notes__)
        assert raised.value.__notes__ == ["Cleanup diagnostic capture failed (RuntimeError)."]

    asyncio.run(run())


def test_cleanup_failure_keeps_scenario_failure_and_remains_fatal_on_success() -> None:
    async def run() -> None:
        scenario_error = AssertionError("scenario failed")

        async def cleanup() -> None:
            raise RuntimeError("cleanup failed")

        async def trigger_cleanup_with_scenario_failure() -> None:
            try:
                raise scenario_error
            except Exception as error:
                await cleanup_without_masking(error, cleanup)
                raise

        with pytest.raises(AssertionError, match="scenario failed") as raised:
            await trigger_cleanup_with_scenario_failure()

        assert raised.value is scenario_error
        assert raised.value.__notes__ == ["Sandbox cleanup also failed (RuntimeError)."]

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await cleanup_without_masking(None, cleanup)

    asyncio.run(run())


@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_late_repeated_cancellation_preserves_body_error_and_cleanup_cause(
    body_fails: bool, cleanup_fails: bool
) -> None:
    original = AssertionError("scenario failed")
    failure = RuntimeError("cleanup failed")
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        if cleanup_fails:
            raise failure

    async def scenario() -> None:
        if body_fails:
            try:
                raise original
            finally:
                await cleanup_without_masking(original, cleanup)
        else:
            await cleanup_without_masking(None, cleanup)

    owner = asyncio.create_task(scenario())
    await started.wait()
    for _ in range(3):
        owner.cancel()
        delivered = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(delivered.set_result, None)
        await delivered
        assert not owner.done()
    release.set()
    if body_fails:
        with pytest.raises(AssertionError) as caught_body:
            await owner
        assert caught_body.value is original
        if cleanup_fails:
            assert original.__notes__ == ["Sandbox cleanup also failed (RuntimeError)."]
    else:
        with pytest.raises(asyncio.CancelledError) as caught_cancel:
            await owner
        assert caught_cancel.value.__cause__ is (failure if cleanup_fails else None)
