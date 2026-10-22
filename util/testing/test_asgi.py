import asyncio
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from util.testing import asgi

HOST = "127.0.0.1"


def _assert_port_can_be_rebound(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, port))


async def test_the_app_answers_from_the_callers_event_loop() -> None:
    served_on: list[asyncio.AbstractEventLoop] = []

    async def ping(_request: Request) -> PlainTextResponse:
        served_on.append(asyncio.get_running_loop())
        return PlainTextResponse("pong")

    async with asgi.serve_app_in_loop(Starlette(routes=[Route("/", ping)])) as origin, httpx.AsyncClient() as client:
        response = await client.get(origin)

    assert (response.text, served_on) == ("pong", [asyncio.get_running_loop()])


async def test_thread_helper_yields_its_ready_origin() -> None:
    async def ping(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("pong")

    async with asgi.serve_app(Starlette(routes=[Route("/", ping)])) as origin, httpx.AsyncClient() as client:
        response = await client.get(origin)

    assert response.text == "pong"


async def test_exit_runs_the_apps_shutdown_and_stops_listening() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        events.append("startup")
        yield
        events.append("shutdown")

    with asgi.bind_http_server() as endpoint:
        async with endpoint.serve_in_loop(Starlette(lifespan=lifespan)) as origin:
            assert events == ["startup"]

        assert events == ["startup", "shutdown"]
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(origin)


async def test_a_server_that_fails_to_start_releases_its_socket() -> None:
    async def refuses_to_start(_scope: Scope, receive: Receive, send: Send) -> None:
        await receive()  # the lifespan.startup message
        await send({"type": "lifespan.startup.failed", "message": "test startup failure"})

    with asgi.bind_http_server() as endpoint:
        with pytest.raises(RuntimeError, match="uvicorn exited"):
            async with endpoint.serve_in_loop(refuses_to_start):
                pytest.fail("the body must not run when the server never started")

        _assert_port_can_be_rebound(HOST, endpoint.port)
        with pytest.raises(RuntimeError, match="only be served once"):
            async with endpoint.serve_in_loop(Starlette()):
                pytest.fail("the bound server is single-use")


async def test_server_configuration_failure_releases_the_bound_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_config(*_args: object, **_kwargs: object) -> uvicorn.Config:
        raise ValueError("test configuration failure")

    monkeypatch.setattr(asgi.uvicorn, "Config", fail_config)
    with asgi.bind_http_server() as endpoint:
        with pytest.raises(ValueError, match="test configuration failure"):
            async with endpoint.serve_in_loop(Starlette()):
                pytest.fail("configuration failed before the app could serve")

        _assert_port_can_be_rebound(HOST, endpoint.port)


async def test_a_server_that_neither_starts_nor_exits_times_out_and_is_torn_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_finishes_starting(_scope: Scope, receive: Receive, _send: Send) -> None:
        await receive()  # the lifespan.startup message, never answered
        await asyncio.Event().wait()

    monkeypatch.setattr(asgi, "_START_TIMEOUT_SECS", 0.2)
    with asgi.bind_http_server() as endpoint:
        with pytest.raises(TimeoutError):
            async with endpoint.serve_in_loop(never_finishes_starting):
                pytest.fail("the body must not run when the server never started")

        _assert_port_can_be_rebound(HOST, endpoint.port)


async def test_cancelling_startup_releases_the_socket() -> None:
    startup_entered = asyncio.Event()

    async def hangs_during_startup(_scope: Scope, receive: Receive, _send: Send) -> None:
        await receive()
        startup_entered.set()
        await asyncio.Event().wait()

    with asgi.bind_http_server() as endpoint:

        async def serve_until_cancelled() -> None:
            async with endpoint.serve_in_loop(hangs_during_startup):
                pytest.fail("the app never completed startup")

        serving = asyncio.create_task(serve_until_cancelled())
        await asyncio.wait_for(startup_entered.wait(), timeout=1)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, timeout=2)

        _assert_port_can_be_rebound(HOST, endpoint.port)


async def test_thread_startup_timeout_cancels_the_app_and_joins_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = threading.Event()

    async def startup_never_finishes(_scope: Scope, receive: Receive, _send: Send) -> None:
        await receive()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(asgi, "_START_TIMEOUT_SECS", 0.1)
    monkeypatch.setattr(asgi, "_SHUTDOWN_TIMEOUT_SECS", 0.1)
    with asgi.bind_http_server() as endpoint:
        with pytest.raises(TimeoutError):
            async with endpoint.serve(startup_never_finishes):
                pytest.fail("startup never finished")
        assert stopped.is_set()
        _assert_port_can_be_rebound(HOST, endpoint.port)


async def test_cancelling_thread_startup_stops_the_app() -> None:
    started = threading.Event()
    stopped = threading.Event()

    async def startup_never_finishes(_scope: Scope, receive: Receive, _send: Send) -> None:
        await receive()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with asgi.bind_http_server() as endpoint:

        async def run() -> None:
            async with endpoint.serve(startup_never_finishes):
                pytest.fail("startup never finished")

        serving = asyncio.create_task(run())
        assert await asyncio.to_thread(started.wait, 2)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, timeout=2)
        assert stopped.is_set()
        _assert_port_can_be_rebound(HOST, endpoint.port)


def test_wildcard_http_bind_requires_a_client_address() -> None:
    with pytest.raises(ValueError, match="advertised_host"), asgi.bind_http_server(host="0.0.0.0"):
        pytest.fail("a wildcard bind must not publish an unusable origin")

    with asgi.bind_http_server(host="0.0.0.0", advertised_host="test-host.invalid") as endpoint:
        assert endpoint.origin == f"http://test-host.invalid:{endpoint.port}"


async def test_two_origins_can_be_used_to_build_mutually_configured_apps() -> None:
    with asgi.bind_http_server() as idp, asgi.bind_http_server() as app_server:
        idp_origin, app_origin = idp.origin, app_server.origin

        async def peer(_request: Request) -> PlainTextResponse:
            return PlainTextResponse(app_origin)

        async with (
            idp.serve(Starlette(routes=[Route("/app", peer)])),
            app_server.serve(Starlette(routes=[Route("/", lambda _request: PlainTextResponse("ready"))])),
            httpx.AsyncClient() as client,
        ):
            peer_response = await client.get(f"{idp_origin}/app")
            app_response = await client.get(peer_response.text)

    assert (peer_response.text, app_response.text) == (app_origin, "ready")
    assert (idp.origin, app_server.origin) == (idp_origin, app_origin)


def test_sync_helper_yields_a_ready_origin() -> None:
    async def ping(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("pong")

    with asgi.serve_app_sync(Starlette(routes=[Route("/", ping)])) as origin, httpx.Client() as client:
        response = client.get(origin)

    assert response.text == "pong"


def test_fastmcp_helper_yields_the_mcp_path() -> None:
    with asgi.serve_fastmcp(FastMCP("test")) as url:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")


if __name__ == "__main__":
    pytest_bazel.main()
