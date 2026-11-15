"""Cancellation-safe completion of owned asynchronous cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass


@dataclass(frozen=True)
class CleanupResult:
    failure: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None


async def finish_cleanup(work: Awaitable[object]) -> CleanupResult:
    """Finish cleanup despite caller cancellation and report both outcomes separately."""

    async def run() -> None:
        await work

    closing = asyncio.create_task(run())
    caller = asyncio.current_task()
    observed_cancellations = caller.cancelling() if caller is not None else 0
    cancellation: asyncio.CancelledError | None = None

    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as error:
            current_cancellations = caller.cancelling() if caller is not None else observed_cancellations
            if current_cancellations > observed_cancellations:
                cancellation = cancellation or error
                observed_cancellations = current_cancellations
            else:
                # A cancelled cleanup task is a cleanup failure, not caller cancellation.
                break
        except BaseException:
            break

    failure: BaseException | None = None
    try:
        closing.result()
    except BaseException as error:
        failure = error

    return CleanupResult(failure=failure, cancellation=cancellation)
