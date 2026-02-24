"""Reusable CLI decorators."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any


def async_run(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to run an async Typer command via asyncio.run (DRY).

    Usage:
        @app.command()
        @async_run
        async def cmd_example(...) -> None:
            await some_async_operation()
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return _wrapper
