"""Async model endpoints driven through typed request exchanges.

The native harness sees an ordinary loopback HTTP endpoint.  Tests see only a
parsed API request and an exchange that owns its response lifecycle; HTTP
objects and sockets stay inside this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import web
from pydantic import BaseModel


@dataclass(frozen=True)
class JsonResponse:
    """One complete non-streaming model API response."""

    content: bytes
    status: int = 200
    content_type: str = "application/json"


@dataclass(frozen=True)
class SseEvent:
    """One complete server-sent event emitted by a scripted model response."""

    kind: str
    data: bytes


@dataclass(frozen=True)
class _Send:
    event: SseEvent
    complete: asyncio.Future[None]


@dataclass(frozen=True)
class _Respond:
    response: JsonResponse
    complete: asyncio.Future[None]


@dataclass(frozen=True)
class _Close:
    complete: asyncio.Future[None]


@dataclass(frozen=True)
class _Abort:
    complete: asyncio.Future[None]


_Action = _Send | _Respond | _Close | _Abort


class _HttpExchange:
    def __init__(self, request: web.Request):
        self._request = request
        self._actions: asyncio.Queue[_Action] = asyncio.Queue()
        self._closed = asyncio.Event()
        self._action_waiters: set[asyncio.Future[None]] = set()

    async def submit(self, action: _Action) -> None:
        if self._closed.is_set():
            raise ConnectionError("the native harness already closed this model request")
        self._action_waiters.add(action.complete)
        await self._actions.put(action)
        try:
            await action.complete
        finally:
            self._action_waiters.discard(action.complete)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def serve(self) -> web.StreamResponse:
        stream: web.StreamResponse | None = None
        try:
            while True:
                action = await self._actions.get()
                if isinstance(action, _Respond):
                    response = web.Response(
                        body=action.response.content,
                        status=action.response.status,
                        content_type=action.response.content_type,
                    )
                    action.complete.set_result(None)
                    return response
                if isinstance(action, _Abort):
                    action.complete.set_result(None)
                    transport = self._request.transport
                    if transport is not None:
                        # A lost SSE stream is a partial response followed by a close, like the
                        # former socket endpoint.  `abort()` sends a TCP reset; Claude does not
                        # take its retry path for that transport failure.
                        transport.close()
                    raise ConnectionError("scripted model endpoint closed the request mid-response")
                if stream is None:
                    stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                    # The former test endpoint used connection-delimited SSE, including for a
                    # truncated stream.  Keep that wire shape: HTTP/1.1 chunking turns the same
                    # close into a different failure that Claude does not retry.
                    stream.force_close()
                    stream._length_check = False
                    await stream.prepare(self._request)
                if isinstance(action, _Send):
                    await stream.write(action.event.data)
                    action.complete.set_result(None)
                    continue
                action.complete.set_result(None)
                await stream.write_eof()
                return stream
        except (asyncio.CancelledError, ConnectionError) as error:
            for waiter in self._action_waiters:
                if not waiter.done():
                    waiter.set_exception(ConnectionError("native harness closed the model request"))
            raise error
        finally:
            self._closed.set()


class ModelExchange[RequestT: BaseModel]:
    """A parsed request and the only authority for its scripted response."""

    def __init__(self, request: RequestT, http: _HttpExchange):
        self.request = request
        self._http = http
        self._settled_by: str | None = None

    async def respond(self, response: JsonResponse) -> None:
        self._ensure_open("respond")
        await self._http.submit(_Respond(response, asyncio.get_running_loop().create_future()))
        self._settled_by = "respond"

    async def send(self, *events: SseEvent) -> None:
        self._ensure_open("send")
        for event in events:
            await self._http.submit(_Send(event, asyncio.get_running_loop().create_future()))

    async def close(self) -> None:
        self._ensure_open("close")
        await self._http.submit(_Close(asyncio.get_running_loop().create_future()))
        self._settled_by = "close"

    async def abort(self) -> None:
        self._ensure_open("abort")
        await self._http.submit(_Abort(asyncio.get_running_loop().create_future()))
        self._settled_by = "abort"

    async def wait_client_closed(self) -> None:
        self._ensure_open("wait for the client to close")
        await self._http.wait_closed()
        self._settled_by = "client close"

    def _ensure_open(self, action: str) -> None:
        if self._settled_by is not None:
            raise RuntimeError(f"cannot {action}: exchange already settled by {self._settled_by}")


class ModelEndpoint[RequestT: BaseModel]:
    """aiohttp lifecycle shared by concrete wire-shaped endpoint fixtures."""

    def __init__(self) -> None:
        self._pending: asyncio.Queue[ModelExchange[RequestT]] = asyncio.Queue()
        self._exchanges: list[ModelExchange[RequestT]] = []
        self._runner: web.AppRunner | None = None
        self._origin: str | None = None

    @property
    def origin(self) -> str:
        if self._origin is None:
            raise RuntimeError("model endpoint has not started")
        return self._origin

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/{tail:.*}", self._post)
        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        address = cast(tuple[str, int], self._runner.addresses[0])
        self._origin = f"http://{address[0]}:{address[1]}"

    async def stop(self) -> None:
        try:
            unsettled = [exchange for exchange in self._exchanges if exchange._settled_by is None]
            if unsettled:
                raise AssertionError(f"unsettled model exchanges: {len(unsettled)}")
        finally:
            if self._runner is not None:
                await self._runner.cleanup()

    async def _next_exchange(self) -> ModelExchange[RequestT]:
        return await self._pending.get()

    async def _post(self, request: web.Request) -> web.StreamResponse:
        try:
            exchange = ModelExchange(self._parse_request(await request.json()), _HttpExchange(request))
        except Exception as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        self._exchanges.append(exchange)
        await self._pending.put(exchange)
        return await exchange._http.serve()

    def _parse_request(self, value: Any) -> RequestT:
        raise NotImplementedError
