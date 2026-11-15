"""Drain asynchronous cleanup before propagating cancellation to its owner."""

import asyncio
from collections.abc import Awaitable


async def finish_cleanup[T](cleanup: Awaitable[T]) -> T:
    # shield() alone still cancels its caller. Keep awaiting the same owned task,
    # including after a second cancellation, so the deployment outlives its guests.
    closing = asyncio.ensure_future(cleanup)
    cancellation: asyncio.CancelledError | None = None
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as error:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                cancellation = cancellation or error
        except BaseException:
            break

    if cancellation is not None:
        try:
            closing.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation
    return closing.result()
