"""Cancellation-safe completion of owned asynchronous cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass


class CleanupError(RuntimeError):
    """Owned resources could not be released; further allocations are unsafe."""


async def close_resources(resources: AsyncExitStack, error: BaseException | None = None) -> None:
    result = await finish_cleanup(resources.aclose())
    if result.failure is not None:
        cause = (
            result.failure if error is None else BaseExceptionGroup("Case and cleanup failed", [error, result.failure])
        )
        raise CleanupError("Owned test resources could not be released") from cause
    if error is None:
        result.raise_outcome()


@asynccontextmanager
async def resource_scope() -> AsyncIterator[AsyncExitStack]:
    async with AsyncExitStack() as resources:
        error: BaseException | None = None
        try:
            yield resources
        except BaseException as caught:
            error = caught
            raise
        finally:
            # No body exception enters the resource exits: failed deletion must remain visible.
            await close_resources(resources.pop_all(), error)


@dataclass(frozen=True)
class CleanupResult:
    failure: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None

    def raise_outcome(self) -> None:
        """Raise the caller's deferred cancellation, chained from any cleanup failure, else the failure."""
        if self.cancellation is not None:
            # A cleanup that re-raised the caller's own cancellation must not chain it to itself.
            if self.failure is not None and self.failure is not self.cancellation:
                raise self.cancellation from self.failure
            raise self.cancellation
        if self.failure is not None:
            raise self.failure

    def propagate(self, primary: BaseException | None, *, note: str) -> None:
        """Keep `primary` primary with a content-free `note` on a cleanup failure; without one, raise the outcome.

        The note names only the failure's type, so cleanup error text never reaches the primary's report.
        """
        if primary is None:
            self.raise_outcome()
        elif self.failure is not None:
            kind = type(self.failure).__name__
            if isinstance(self.failure, BaseExceptionGroup):
                kind = f"{kind} of {len(self.failure.exceptions)}"
            primary.add_note(f"{note} ({kind}).")


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
