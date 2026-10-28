import asyncio

import pytest
import pytest_bazel

from agentplane.system_tests.cleanup_diagnostics import capture_without_masking, cleanup_without_masking


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


if __name__ == "__main__":
    pytest_bazel.main()
