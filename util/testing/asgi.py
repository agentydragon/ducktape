"""Serve an ASGI app on a real local port for tests.

Some flows can't run against an in-memory transport — a server-to-server OAuth token exchange, an
MCP client that follows cross-origin redirects, anything that needs a real socket. ``serve_app``
runs the app under uvicorn in a daemon thread and yields once it's accepting connections.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from starlette.types import ASGIApp


@asynccontextmanager
async def serve_app(app: ASGIApp, *, port: int):
    """Start a uvicorn server in a dedicated thread; yield when ready; shut down on exit."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread exited before starting")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=3.0)
            raise TimeoutError(f"server did not start on port {port}")
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=3.0)
