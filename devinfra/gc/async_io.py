"""Run blocking filesystem and libgit2 work without abandoning it on cancellation."""

import asyncio
from collections.abc import Callable
from contextlib import suppress


async def run_blocking[**P, T](func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    thread = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(thread)
    except asyncio.CancelledError:
        # Cancellation wins over errors from the abandoned read.
        with suppress(Exception):
            await thread
        raise
