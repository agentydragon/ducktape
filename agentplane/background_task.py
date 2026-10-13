"""Background tasks that run for the duration of a block."""

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None], *, name: str) -> AsyncIterator[None]:
    """Runs `coroutine` for the duration of the block and cancels it on exit. A failure is logged when
    it happens and leaves the block running."""
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(_report_exit)
    try:
        yield
    finally:
        task.cancel()
        await asyncio.wait([task])


def _report_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s stopped", task.get_name(), exc_info=error)
