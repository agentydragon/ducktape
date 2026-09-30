import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_bazel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from util.net import bind_free_port
from util.testing import asgi


@pytest.fixture
def sock() -> Iterator[socket.socket]:
    with bind_free_port() as sock:
        yield sock


def _url(sock: socket.socket) -> str:  # before the server stops: uvicorn closes the socket on shutdown
    return f"http://127.0.0.1:{sock.getsockname()[1]}"


async def test_the_app_answers_from_the_callers_event_loop(sock: socket.socket) -> None:
    served_on: list[asyncio.AbstractEventLoop] = []

    async def ping(_request: Request) -> PlainTextResponse:
        served_on.append(asyncio.get_running_loop())
        return PlainTextResponse("pong")

    url = _url(sock)
    async with asgi.serve_app_in_loop(Starlette(routes=[Route("/", ping)]), sock=sock), httpx.AsyncClient() as client:
        response = await client.get(url)

    assert (response.text, served_on) == ("pong", [asyncio.get_running_loop()])


async def test_exit_runs_the_apps_shutdown_and_stops_listening(sock: socket.socket) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        events.append("startup")
        yield
        events.append("shutdown")

    url = _url(sock)
    async with asgi.serve_app_in_loop(Starlette(lifespan=lifespan), sock=sock):
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(url)


async def test_a_server_that_fails_to_start_raises_instead_of_hanging(sock: socket.socket) -> None:
    # uvicorn answers a failed startup with SystemExit, which would otherwise end the test's whole event loop.
    async def refuses_to_start(_scope: Scope, receive: Receive, send: Send) -> None:
        await receive()  # the lifespan.startup message
        await send({"type": "lifespan.startup.failed", "message": "test startup failure"})

    with pytest.raises(RuntimeError, match="uvicorn exited"):
        async with asgi.serve_app_in_loop(refuses_to_start, sock=sock):
            pytest.fail("the body must not run when the server never started")


async def test_a_server_that_neither_starts_nor_exits_times_out_and_is_torn_down(
    sock: socket.socket, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never_finishes_starting(_scope: Scope, receive: Receive, _send: Send) -> None:
        await receive()  # the lifespan.startup message, never answered
        await asyncio.Event().wait()

    monkeypatch.setattr(asgi, "_START_TIMEOUT_SECS", 0.2)
    with pytest.raises(TimeoutError):
        async with asgi.serve_app_in_loop(never_finishes_starting, sock=sock):
            pytest.fail("the body must not run when the server never started")


if __name__ == "__main__":
    pytest_bazel.main()
