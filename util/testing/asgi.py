"""Serve an ASGI app on a real local port for tests.

Some flows can't run against an in-memory transport — a server-to-server OAuth token exchange, an
MCP client that follows cross-origin redirects, anything that needs a real socket. ``serve_app``
(async) and ``serve_app_sync`` (sync, yields the base URL) run the app under uvicorn in a daemon
thread and hand back once it's accepting connections; ``serve_fastmcp`` mounts a ``FastMCP`` at
``/mcp`` and serves it the same way.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp

from util.net import pick_free_port, wait_for_port


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


@contextmanager
def serve_app_sync(app: ASGIApp, *, port: int | None = None) -> Generator[str]:
    """Sync sibling of ``serve_app``: serve ``app`` under uvicorn in a daemon thread and yield its
    base URL (``http://127.0.0.1:{port}``), choosing a free port when none is given. For tests that
    need a real socket outside an async context, or that want the URL handed back."""
    port = port or pick_free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for_port("127.0.0.1", port, timeout_secs=10)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError(f"uvicorn server on port {port} did not stop")


@contextmanager
def serve_fastmcp(server: FastMCP, *, port: int | None = None) -> Generator[str]:
    """Serve a ``FastMCP`` over streamable HTTP (mounted at ``/mcp``) and yield its ``.../mcp`` URL."""
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    with serve_app_sync(app, port=port) as base:
        yield f"{base}/mcp"
