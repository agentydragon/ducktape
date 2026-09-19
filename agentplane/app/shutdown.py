"""The fence a SIGTERM raises, and what observes it: admission, readiness, and every open stream.

Uvicorn bounds its wait for open requests with `timeout_graceful_shutdown` and cancels whatever is
still running when that runs out, but a cancelled stream is an aborted response and the budget was
spent waiting for it. The drain ends the streams instead, at the signal, so the budget is a cap the
app stays well under; `main.py` begins it as the server's shutdown starts.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class Drain:
    """Begun once, at shutdown, and never cleared."""

    def __init__(self) -> None:
        self._begun = asyncio.Event()

    @property
    def draining(self) -> bool:
        return self._begun.is_set()

    def begin(self) -> None:
        self._begun.set()

    async def until[T](self, source: AsyncIterator[T]) -> AsyncIterator[T]:
        """`source`'s items until the drain begins, which ends the stream wherever it is waiting -- a
        live view waiting out a quiet quarter-minute ends now, not at its next health frame."""
        while True:
            next_item = asyncio.ensure_future(anext(source))
            begun = asyncio.ensure_future(self._begun.wait())
            try:
                await asyncio.wait({next_item, begun}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                begun.cancel()
                if not next_item.done():
                    # Cancelling the pending `anext` throws into `source` at its await, which closes it.
                    next_item.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await next_item
            if next_item.cancelled():
                return
            try:
                item = next_item.result()
            except StopAsyncIteration:
                return
            yield item


class DrainMiddleware:
    """503 to every request but the liveness probe's once the drain has begun.

    Readiness fails first of all; what is already answering finishes. The liveness probe keeps
    passing so the kubelet does not restart a container that is only shutting down.
    """

    def __init__(self, app: ASGIApp, *, drain: Drain, liveness_path: str) -> None:
        self._app = app
        self._drain = drain
        self._liveness_path = liveness_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._drain.draining and scope["path"] != self._liveness_path:
            response = JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "the app is shutting down"}
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def drain_of(app: FastAPI) -> Drain:
    drain = app.state.drain
    if not isinstance(drain, Drain):
        raise TypeError(f"app.state.drain is {type(drain).__name__}, not Drain")
    return drain


def _drain(request: Request) -> Drain:
    return drain_of(request.app)


Shutdown = Annotated[Drain, Depends(_drain)]
