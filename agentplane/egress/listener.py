"""Uvicorn hosted inside the proxy process, for the rules API and the admin port alike."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from fastapi import FastAPI
from more_itertools import one


class _EmbeddedServer(uvicorn.Server):
    """The proxy's outer lifecycle owns signal handling; `started` is announced as an event."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            self.ready.set()


@asynccontextmanager
async def serve(app: FastAPI, host: str, port: int) -> AsyncIterator[int]:
    """Serve `app` until the central process shuts down, yielding the bound port once it accepts
    connections (pass 0 for an ephemeral one)."""
    server = _EmbeddedServer(uvicorn.Config(app, host=host, port=port, access_log=False, timeout_graceful_shutdown=5))
    serving = asyncio.create_task(server.serve(), name=f"egress-listener-{app.title}")
    ready = asyncio.create_task(server.ready.wait())
    try:
        await asyncio.wait([serving, ready], return_when=asyncio.FIRST_COMPLETED)
        if not ready.done():
            await serving
            raise RuntimeError(f"{app.title} exited before accepting connections")
        yield one(one(server.servers).sockets).getsockname()[1]
    finally:
        ready.cancel()
        server.should_exit = True
        await serving
