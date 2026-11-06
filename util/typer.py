"""Reusable CLI decorators."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any


def async_run(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run an async Typer command via asyncio.run; apply below @app.command()."""

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return _wrapper
