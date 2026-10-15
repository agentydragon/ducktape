"""Single-session, in-memory RemoteIO peer for scripted interoperability experiments."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

SESSION_PATH = "/v1/code/sessions/remote-io-probe"
TEST_TOKEN = "remote-io-synthetic-token"


class RemoteIOServer:
    def __init__(self, trace: Path):
        self.trace = trace
        self.commands: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.changed = asyncio.Condition()
        self.stopping = False
        self.epoch = 1
        self.connected = asyncio.Event()
        self.stream_requests: list[web.Request] = []
        self.stream_cursors: list[int] = []
        self.app = web.Application()
        self.app.router.add_route("*", SESSION_PATH + "/{tail:.*}", self.handle)

    def record(self, direction: str, path: str, body: object) -> None:
        # Deliberately exclude headers: even this synthetic peer must not teach credential logging.
        with self.trace.open("a") as stream:
            stream.write(json.dumps({"direction": direction, "path": path, "body": body}) + "\n")

    async def send(self, payload: dict[str, Any]) -> str:
        event_id = str(uuid4())
        async with self.changed:
            event = {
                "event_id": event_id,
                "sequence_num": len(self.commands) + 1,
                "event_type": payload["type"],
                "payload": payload,
            }
            self.commands.append(event)
            self.record("server", "client_event", event)
            self.changed.notify_all()
        return event_id

    async def wait_for(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        async with self.changed:
            await self.changed.wait_for(lambda: any(predicate(item) for item in self.uploads))
            return next(item for item in self.uploads if predicate(item))

    async def begin_incarnation(self) -> None:
        """Script a new worker after the prior process died; do not replay settled commands."""
        async with self.changed:
            self.epoch += 1
            self.commands.clear()
            self.connected.clear()
            for request in self.stream_requests:
                if request.transport is not None:
                    request.transport.close()
            self.changed.notify_all()
            self.record("server", "begin_incarnation", {"worker_epoch": self.epoch})

    async def reconnect(self) -> int:
        async with self.changed:
            previous = len(self.stream_requests)
            for request in self.stream_requests:
                if request.transport is not None:
                    request.transport.close()
            await self.changed.wait_for(lambda: len(self.stream_requests) > previous)
            return self.stream_cursors[-1]

    async def close(self) -> None:
        async with self.changed:
            self.stopping = True
            self.changed.notify_all()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {TEST_TOKEN}":
            raise web.HTTPUnauthorized
        tail = request.match_info["tail"]
        if request.method == "GET" and tail == "worker/events/stream":
            return await self.events(request)
        try:
            body = await request.json() if request.can_read_body else None
        except json.JSONDecodeError as error:
            raise web.HTTPBadRequest(text="invalid JSON body") from error
        self.record("worker", request.path_qs, body)
        if request.method == "GET" and tail == "worker":
            return web.json_response({"worker": {"external_metadata": {}, "internal_metadata": {}}})
        if request.method == "GET" and tail == "worker/internal-events":
            # No hydration in the baseline: do not manufacture history for the CLI.
            return web.json_response({"data": []})
        if request.method == "POST" and tail == "worker/register":
            return web.json_response({"worker_epoch": self.epoch})
        if request.method in {"PUT", "POST"} and tail in {
            "worker",
            "worker/events",
            "worker/events/delivery",
            "worker/internal-events",
            "worker/heartbeat",
        }:
            if not isinstance(body, dict):
                raise web.HTTPBadRequest
            if body.get("worker_epoch") != self.epoch:
                return web.json_response({"reason": "worker_epoch_mismatch"}, status=409)
            async with self.changed:
                self.uploads.append({"path": tail, "body": body})
                self.changed.notify_all()
            return web.json_response({"has_subscribers": True})
        raise web.HTTPNotFound

    async def events(self, request: web.Request) -> web.StreamResponse:
        try:
            cursor = int(request.query.get("from_sequence_num", request.headers.get("Last-Event-ID", "0")))
        except ValueError as error:
            raise web.HTTPBadRequest(text="invalid event cursor") from error
        if cursor < 0:
            raise web.HTTPBadRequest(text="negative event cursor")
        self.record("worker", request.path_qs, {"cursor": cursor})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b": connected\n\n")
        async with self.changed:
            self.stream_requests.append(request)
            self.stream_cursors.append(cursor)
            self.connected.set()
            self.changed.notify_all()

        epoch = self.epoch

        def ready() -> bool:
            return self.stopping or epoch != self.epoch or len(self.commands) > cursor

        try:
            while not self.stopping:
                async with self.changed:
                    await self.changed.wait_for(ready)
                    if epoch != self.epoch:
                        return response
                    pending = self.commands[cursor:]
                for event in pending:
                    data = json.dumps(event)
                    await response.write(f"id: {event['sequence_num']}\nevent: client_event\ndata: {data}\n\n".encode())
                    cursor = event["sequence_num"]
        except ConnectionResetError:
            pass
        return response
