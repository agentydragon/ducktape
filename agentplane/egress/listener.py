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
        if not self.started:
            raise RuntimeError("Embedded server exited before accepting connections")
        self.ready.set()


@asynccontextmanager
async def serve(app: FastAPI, host: str, port: int) -> AsyncIterator[int]:
    """Serve `app` until the central process shuts down, yielding the bound port once it accepts
    connections (pass 0 for an ephemeral one)."""
    server = _EmbeddedServer(uvicorn.Config(app, host=host, port=port, access_log=False, timeout_graceful_shutdown=5))
    async with asyncio.TaskGroup() as tasks:
        serving = tasks.create_task(server.serve(), name=f"egress-listener-{app.title}")
        try:
            await server.ready.wait()
            yield one(one(server.servers).sockets).getsockname()[1]
        finally:
            server.should_exit = True
            # Let Uvicorn drain connections and stop the app before TaskGroup's
            # exceptional exit would cancel it. The group propagates server errors.
            await asyncio.wait({serving})
