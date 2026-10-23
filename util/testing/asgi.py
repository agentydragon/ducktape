"""Serve ASGI apps on owned TCP listeners for tests.

Use ``serve_app`` for a self-contained app, ``serve_app_in_loop`` when it shares
loop-bound resources with the test, and ``serve_app_sync`` from synchronous tests
and fixtures. Use ``bind_http_server`` when an app's origin must be known before
the app is constructed. These helpers wait for Uvicorn startup, including ASGI
lifespan startup, before yielding the origin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import time
from collections.abc import AsyncIterator, Generator
from concurrent.futures import Future
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp

from util.net import Listener, listen_tcp

_START_TIMEOUT_SECS = 10.0
_START_POLL_SECS = 0.02
_SHUTDOWN_TIMEOUT_SECS = 5.0
_FORCE_STOP_TIMEOUT_SECS = 3.0


def _is_wildcard(host: str) -> bool:
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _origin_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


class BoundHTTPServer:
    """Own a bound HTTP socket and allow one Uvicorn serving lifetime."""

    def __init__(self, listener: Listener, *, advertised_host: str | None) -> None:
        self._socket = listener.socket
        self.port = listener.port
        self._closed = False
        self._used = False
        self.origin = f"http://{_origin_host(advertised_host or listener.host)}:{self.port}"

    def _claim(self) -> socket.socket:
        if self._closed:
            raise RuntimeError("bound HTTP server is closed")
        if self._used:
            raise RuntimeError("bound HTTP server can only be served once")
        self._used = True
        return self._socket

    def _close(self) -> None:
        self._closed = True
        self._socket.close()

    @asynccontextmanager
    async def serve(self, app: ASGIApp) -> AsyncIterator[str]:
        """Serve the app in a daemon thread and yield its ready origin."""
        sock = self._claim()
        serving: tuple[uvicorn.Server, _ServerThread] | None = None
        try:
            serving = await _start_in_thread(app, sock)
            yield self.origin
        finally:
            if serving is not None:
                await _stop_in_thread(*serving)
            sock.close()

    @asynccontextmanager
    async def serve_in_loop(self, app: ASGIApp) -> AsyncIterator[str]:
        """Serve the app as a task in the current event loop and yield its ready origin."""
        sock = self._claim()
        server: uvicorn.Server | None = None
        serving: asyncio.Task[None] | None = None
        startup_failed = False
        try:
            server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning"))
            serving = asyncio.create_task(_serve_in_loop(server, sock))
            try:
                await _wait_until_started(server, serving)
            except BaseException:
                startup_failed = True
                stopping = asyncio.create_task(_cancel_in_loop(serving))
                try:
                    await asyncio.shield(stopping)
                except asyncio.CancelledError:
                    await asyncio.shield(stopping)
                    raise
                raise
            yield self.origin
        finally:
            if server is not None and serving is not None and not serving.done():
                stopping = asyncio.create_task(_stop_in_loop(server, serving))
                try:
                    await asyncio.shield(stopping)
                except asyncio.CancelledError:
                    await asyncio.shield(stopping)
                    raise
            elif serving is not None and not startup_failed and not serving.cancelled():
                serving.result()
            sock.close()

    @contextmanager
    def serve_sync(self, app: ASGIApp) -> Generator[str]:
        """Serve the app in a daemon thread from a synchronous context."""
        sock = self._claim()
        serving: tuple[uvicorn.Server, _ServerThread] | None = None
        try:
            serving = _start(app, sock)
            yield self.origin
        finally:
            if serving is not None:
                _stop(*serving)
            sock.close()


@contextmanager
def bind_http_server(host: str = "127.0.0.1", *, advertised_host: str | None = None) -> Generator[BoundHTTPServer]:
    """Bind an HTTP endpoint before constructing its app; the server may serve it once.

    A wildcard bind requires ``advertised_host`` because wildcard addresses cannot
    be used as client origins. The advertised address is cached before Uvicorn takes
    ownership of the socket.
    """
    if _is_wildcard(host) and not advertised_host:
        raise ValueError("wildcard HTTP binds require advertised_host")
    with listen_tcp(host) as listener:
        endpoint = BoundHTTPServer(listener, advertised_host=advertised_host)
        try:
            yield endpoint
        finally:
            endpoint._close()


class _ServerThread(threading.Thread):
    def __init__(self, server: uvicorn.Server, sock: socket.socket) -> None:
        super().__init__(daemon=True)
        self.server = server
        self.sock = sock
        self.running: Future[tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]] = Future()
        self.failure: BaseException | None = None
        self.cancel_requested = False

    def run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            self.failure = error

    async def _serve(self) -> None:
        serving = asyncio.create_task(_serve_in_loop(self.server, self.sock))
        self.running.set_result((asyncio.get_running_loop(), serving))
        await serving

    def cancel(self) -> None:
        self.cancel_requested = True
        loop, serving = self.running.result(timeout=_FORCE_STOP_TIMEOUT_SECS)
        loop.call_soon_threadsafe(serving.cancel)


def _start(
    app: ASGIApp, sock: socket.socket, *, cancelled: threading.Event | None = None
) -> tuple[uvicorn.Server, _ServerThread]:
    """Run the app under Uvicorn in a daemon thread; return once it is serving."""
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning"))
    thread = _ServerThread(server, sock)
    try:
        thread.start()
        deadline = time.monotonic() + _START_TIMEOUT_SECS
        while not server.started:
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError
            if not thread.is_alive():
                thread.join()
                raise RuntimeError("uvicorn thread exited before starting")
            if time.monotonic() > deadline:
                raise TimeoutError(f"server did not start on {sock.getsockname()}")
            time.sleep(_START_POLL_SECS)
        if not thread.is_alive():
            thread.join()
            raise RuntimeError("uvicorn thread exited during startup")
        return server, thread
    except BaseException:
        if thread.ident is not None and thread.is_alive():
            _stop(server, thread)
        raise


async def _start_in_thread(app: ASGIApp, sock: socket.socket) -> tuple[uvicorn.Server, _ServerThread]:
    cancelled = threading.Event()
    startup = asyncio.create_task(asyncio.to_thread(_start, app, sock, cancelled=cancelled))
    try:
        return await asyncio.shield(startup)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            server, thread = await asyncio.shield(startup)
        except BaseException:
            # The startup worker owns cleanup until it returns a serving thread.
            pass
        else:
            await asyncio.to_thread(_stop, server, thread)
        raise


def _stop(server: uvicorn.Server, thread: _ServerThread) -> None:
    server.should_exit = True
    if not server.started and thread.is_alive():
        thread.cancel()
    thread.join(timeout=_SHUTDOWN_TIMEOUT_SECS)
    if thread.is_alive():
        server.force_exit = True
        thread.cancel()
        thread.join(timeout=_FORCE_STOP_TIMEOUT_SECS)
        if thread.is_alive():
            raise RuntimeError("uvicorn server did not stop")
    if thread.failure is not None and not (
        thread.cancel_requested and isinstance(thread.failure, asyncio.CancelledError)
    ):
        raise RuntimeError("uvicorn server thread failed") from thread.failure


async def _stop_in_thread(server: uvicorn.Server, thread: _ServerThread) -> None:
    stopping = asyncio.create_task(asyncio.to_thread(_stop, server, thread))
    try:
        await asyncio.shield(stopping)
    except asyncio.CancelledError:
        await asyncio.shield(stopping)
        raise


async def _serve_in_loop(server: uvicorn.Server, sock: socket.socket) -> None:
    try:
        await server.serve(sockets=[sock])
    except SystemExit as exc:  # Uvicorn exits on failed startup; do not terminate the test loop.
        raise RuntimeError(f"uvicorn exited with status {exc.code}") from exc


async def _wait_until_started(server: uvicorn.Server, serving: asyncio.Task[None]) -> None:
    async with asyncio.timeout(_START_TIMEOUT_SECS):
        while not server.started:
            if serving.done():
                serving.result()
                raise RuntimeError("uvicorn exited before starting")
            await asyncio.sleep(_START_POLL_SECS)


async def _stop_in_loop(server: uvicorn.Server, serving: asyncio.Task[None]) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(serving), timeout=_SHUTDOWN_TIMEOUT_SECS)
    except TimeoutError:
        serving.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(serving), timeout=_FORCE_STOP_TIMEOUT_SECS)
        except asyncio.CancelledError:
            pass
        except TimeoutError as exc:
            raise RuntimeError("uvicorn server did not stop") from exc


async def _cancel_in_loop(serving: asyncio.Task[None]) -> None:
    serving.cancel()
    done, _pending = await asyncio.wait((serving,), timeout=_FORCE_STOP_TIMEOUT_SECS)
    if not done:
        raise RuntimeError("uvicorn server did not stop after startup was cancelled")
    if not serving.cancelled():
        serving.exception()


@asynccontextmanager
async def serve_app(app: ASGIApp) -> AsyncIterator[str]:
    """Serve a self-contained app on an owned local port in a dedicated thread."""
    with bind_http_server() as endpoint:
        async with endpoint.serve(app) as origin:
            yield origin


@asynccontextmanager
async def serve_app_in_loop(app: ASGIApp) -> AsyncIterator[str]:
    """Serve an app on the caller's event loop for loop-bound resources."""
    with bind_http_server() as endpoint:
        async with endpoint.serve_in_loop(app) as origin:
            yield origin


@contextmanager
def serve_app_sync(app: ASGIApp) -> Generator[str]:
    """Serve an app in a dedicated thread from a synchronous test or fixture."""
    with bind_http_server() as endpoint, endpoint.serve_sync(app) as origin:
        yield origin


@contextmanager
def serve_fastmcp(server: FastMCP) -> Generator[str]:
    """Serve a FastMCP app and yield its streamable HTTP URL, including ``/mcp``."""
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    with serve_app_sync(app) as origin:
        yield f"{origin}/mcp"
