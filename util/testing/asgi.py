"""Serve an ASGI app on a real local port for tests.

Some flows can't run against an in-memory transport — a server-to-server OAuth token exchange, an
MCP client that follows cross-origin redirects, anything that needs a real socket. ``serve_app``
(async) and ``serve_app_sync`` (sync, yields the base URL) run the app under uvicorn in a daemon
thread on a socket that is already bound and listening, and hand back once uvicorn is serving on
it; ``serve_fastmcp`` mounts a ``FastMCP`` at ``/mcp`` and serves it the same way.

Use the thread-based helpers when the app is self-contained. Use ``serve_app_in_loop`` when the
app shares loop-bound resources with the test (an asyncpg engine, an ``asyncio.Event``): it runs
uvicorn as a task in the test's own event loop, which a thread's separate loop cannot.

Readiness is ``server.started``, not a connect probe: a pre-bound listening socket accepts
connections from the kernel backlog before uvicorn has started, so a successful connect proves
nothing about the app.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp

from util.net import bind_free_port

_START_TIMEOUT_SECS = 10.0
_START_POLL_SECS = 0.02


def _start(app: ASGIApp, sock: socket.socket) -> tuple[uvicorn.Server, threading.Thread]:
    """Run ``app`` under uvicorn on ``sock`` in a daemon thread; return once it is serving."""
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + _START_TIMEOUT_SECS
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread exited before starting")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=3.0)
            raise TimeoutError(f"server did not start on {sock.getsockname()}")
        time.sleep(_START_POLL_SECS)
    return server, thread


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5.0)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=3.0)
        if thread.is_alive():
            raise RuntimeError("uvicorn server did not stop")


@asynccontextmanager
async def serve_app(app: ASGIApp, *, sock: socket.socket):
    """Serve ``app`` under uvicorn on ``sock`` -- bound and listening, from ``bind_free_port`` -- in a
    dedicated thread; yield once it's serving; shut down on exit. Taking the socket rather than a
    port number means the port is never released between choosing it and serving on it; uvicorn
    closes the socket on shutdown."""
    server, thread = await asyncio.to_thread(_start, app, sock)
    try:
        yield
    finally:
        _stop(server, thread)


async def _serve_in_loop(server: uvicorn.Server, sock: socket.socket) -> None:
    try:
        await server.serve(sockets=[sock])
    except SystemExit as exc:  # uvicorn exits on a failed startup; asyncio lets that end the whole loop
        raise RuntimeError(f"uvicorn exited with status {exc.code}") from exc


@asynccontextmanager
async def serve_app_in_loop(app: ASGIApp, *, sock: socket.socket) -> AsyncIterator[None]:
    """Sibling of ``serve_app`` that runs uvicorn as a task in the *current* event loop rather than a
    thread, for an app that uses resources bound to this loop. Yields once uvicorn is serving on
    ``sock`` (bound and listening, from ``bind_free_port``); raises if the server ends or does not
    start within ten seconds. On exit, stops the server, waits out its graceful shutdown and
    re-raises its failure, if any. Requests to it must be awaited: a blocking call in the test
    stalls the server."""
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    serving = asyncio.create_task(_serve_in_loop(server, sock))
    try:
        async with asyncio.timeout(_START_TIMEOUT_SECS):
            while not server.started:
                if serving.done():
                    serving.result()
                    raise RuntimeError("uvicorn exited before starting")
                await asyncio.sleep(_START_POLL_SECS)
    except BaseException:
        serving.cancel()  # uvicorn reads should_exit only once its startup returns
        await asyncio.wait([serving])
        raise
    try:
        yield
    finally:
        server.should_exit = True
        await serving


@contextmanager
def serve_app_sync(app: ASGIApp, *, sock: socket.socket | None = None) -> Generator[str]:
    """Sync sibling of ``serve_app``: serve ``app`` under uvicorn in a daemon thread and yield its
    base URL (``http://127.0.0.1:{port}``). Binds a free port itself unless given ``sock`` (bound and
    listening, from ``bind_free_port``) -- pass one when the URL must be known before the app is
    built, as for a ``public_base_url`` setting or an OIDC issuer."""
    if sock is None:
        sock = bind_free_port()
    host, port = sock.getsockname()
    server, thread = _start(app, sock)
    try:
        yield f"http://{host}:{port}"
    finally:
        _stop(server, thread)


@contextmanager
def serve_fastmcp(server: FastMCP, *, sock: socket.socket | None = None) -> Generator[str]:
    """Serve a ``FastMCP`` over streamable HTTP (mounted at ``/mcp``) and yield its ``.../mcp`` URL."""
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    with serve_app_sync(app, sock=sock) as base:
        yield f"{base}/mcp"
