"""Regression test for model-switch admission over a streaming response."""

import asyncio
from contextlib import suppress

import pytest_bazel
from starlette.types import Message, Receive, Scope, Send

from cluster.cdk8s.ollama.tabbyapi_serve import SerializeModelRequests


async def test_queued_generation_waits_for_stream_and_health_remains_available() -> None:
    stream_started = asyncio.Event()
    finish_stream = asyncio.Event()
    next_request_arrived = asyncio.Event()
    next_request_admitted = asyncio.Event()
    health_served = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"] == "/v1/models":
            health_served.set()
        elif scope["path"] == "/v1/completions":
            next_request_admitted.set()
        else:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"data: ...\n\n", "more_body": True})
            stream_started.set()
            await finish_stream.wait()
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    middleware = SerializeModelRequests(app)

    async def request_next_model() -> None:
        next_request_arrived.set()
        await middleware({"type": "http", "method": "POST", "path": "/v1/completions"}, receive, send)

    stream = asyncio.create_task(
        middleware({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, receive, send)
    )
    queued = asyncio.create_task(request_next_model())
    try:
        await asyncio.wait_for(stream_started.wait(), timeout=2)
        await asyncio.wait_for(next_request_arrived.wait(), timeout=2)
        assert not next_request_admitted.is_set()
        await middleware({"type": "http", "method": "GET", "path": "/v1/models"}, receive, send)
        assert health_served.is_set()

        finish_stream.set()
        await asyncio.wait_for(queued, timeout=2)
        assert next_request_admitted.is_set()
    finally:
        for task in (stream, queued):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    pytest_bazel.main()
