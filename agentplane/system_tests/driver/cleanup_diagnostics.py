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
    (await finish_cleanup(cleanup())).propagate(error, note="Sandbox cleanup also failed")
