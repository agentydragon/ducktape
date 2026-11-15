"""Failure-only helpers for preserving cleanup errors when optional evidence capture fails."""

from collections.abc import Awaitable, Callable

from agentplane.system_tests.driver.cleanup import finish_cleanup


async def capture_without_masking(error: Exception, capture: Callable[[], Awaitable[None]]) -> None:
    """Attach a content-free note if diagnostic collection fails, preserving the cleanup error."""
    try:
        await capture()
    except Exception as capture_error:
        error.add_note(f"Cleanup diagnostic capture failed ({type(capture_error).__name__}).")


async def cleanup_without_masking(error: BaseException | None, cleanup: Callable[[], Awaitable[None]]) -> None:
    """Keep a scenario failure primary while allowing cleanup-only failures to fail the test."""
    result = await finish_cleanup(cleanup())
    if result.failure is not None:
        if error is not None:
            error.add_note(f"Sandbox cleanup also failed ({type(result.failure).__name__}).")
        elif result.cancellation is not None:
            raise result.cancellation from result.failure
        else:
            raise result.failure
    if error is None and result.cancellation is not None:
        raise result.cancellation
