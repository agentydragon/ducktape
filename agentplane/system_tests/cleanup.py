"""Keep a system-test failure visible when its Sandbox also resists cleanup."""

import traceback
from collections.abc import Awaitable, Callable


async def cleanup_preserving_failure(cleanup: Callable[[], Awaitable[None]], failure: BaseException | None) -> None:
    try:
        await cleanup()
    except Exception as cleanup_error:
        if failure is None:
            raise
        failure.add_note(
            "Sandbox cleanup also failed:\n" + "".join(traceback.format_exception(cleanup_error, chain=False))
        )
