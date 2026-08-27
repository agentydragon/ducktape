"""Large-payload / streaming integration tests through the embedded proxy (#4914).

The operator's refinement of the large-payload item (issue #4914 comment) is that the
proxy must *stream*, not merely tolerate size: the client has to start receiving a
response body before the upstream has finished serving it, so the proxy never buffers a
whole body. The assertion is on ordering, not elapsed time (STYLE § Waiting): the
upstream sends a prefix, then holds the tail behind a test-controlled gate; the client
reads real body bytes while the gate is still closed, which is impossible if the proxy
buffered the body — a buffering proxy would deliver nothing until the upstream finished,
and the upstream never finishes until the test releases the gate.

Response-direction streaming is enabled by the gate's ``responseheaders`` hook
(haku/egress/addon.py). Request-direction streaming is deliberately not enabled — mitmproxy
dials the upstream and streams a request body before the ``request`` hook runs, which would
let a streamed request reach an already-pinned upstream ahead of its own decision — so a
large request body is exercised on the buffered path here, and streamed-request ordering is
left to the follow-up that moves the gate to ``requestheaders``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest_bazel

from haku.egress.proxy_test_harness import (
    PLACEHOLDER,
    REAL_CREDENTIAL,
    allow,
    allow_with_substitution,
    make_proxy,
    proxy_url,
)
from haku.egress.static_decide_client import StaticDecideClient

UNIT = os.urandom(1 << 20)  # 1 MiB payload unit, reused for every block so memory stays flat
RESPONSE_TOTAL = 256 << 20  # 256 MiB downstream body
PREFIX = 4 << 20  # bytes the upstream serves before gating the tail
REQUEST_TOTAL = 64 << 20  # 64 MiB upstream body (buffered path)


def stream_digest(total: int) -> str:
    """SHA-256 of ``total`` bytes produced by repeating ``UNIT`` — the exact sequence both sides use."""
    hasher = hashlib.sha256()
    pos = 0
    while pos < total:
        hasher.update(UNIT[: min(len(UNIT), total - pos)])
        pos += len(UNIT)
    return hasher.hexdigest()


async def _send_range(writer: asyncio.StreamWriter, start: int, end: int) -> None:
    pos = start
    while pos < end:
        chunk = UNIT[: min(len(UNIT), end - pos)]
        writer.write(chunk)
        pos += len(chunk)
        await writer.drain()  # honour client backpressure rather than buffering the whole tail


@dataclass
class StreamingUpstream:
    """Serves ``total`` bytes but holds everything past ``prefix`` until ``release`` is set.

    ``tail_started`` fires only once the gate is released, so a test can prove the client
    received body bytes while the tail had not begun (the non-buffering ordering property).
    """

    total: int
    prefix: int
    port: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)
    tail_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        writer.write(
            f"HTTP/1.1 200 OK\r\ncontent-type: application/octet-stream\r\n"
            f"content-length: {self.total}\r\nconnection: close\r\n\r\n".encode()
        )
        await _send_range(writer, 0, self.prefix)
        await writer.drain()
        await self.release.wait()
        self.tail_started.set()
        await _send_range(writer, self.prefix, self.total)
        writer.close()


@asynccontextmanager
async def streaming_upstream(total: int, prefix: int) -> AsyncIterator[StreamingUpstream]:
    upstream = StreamingUpstream(total=total, prefix=prefix)
    server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        server.close()
        await server.wait_closed()


async def test_response_streams_before_upstream_finishes(tmp_path: Path) -> None:
    """The client receives real body bytes while the upstream's tail is still gated shut.

    Reading the prefix cannot complete under a buffering proxy — it would deliver nothing until
    the upstream sent the whole body, which never happens until the test releases the gate — so a
    successful read of the prefix before ``tail_started`` is the non-buffering proof, and the
    256 MiB total confirms it holds without a buffering blowup.
    """
    decide = StaticDecideClient(allow())
    async with (
        streaming_upstream(RESPONSE_TOTAL, PREFIX) as up,
        make_proxy(decide, tmp_path) as proxy,
        aiohttp.ClientSession() as session,
        session.get(f"http://127.0.0.1:{up.port}/big", proxy=proxy_url(proxy)) as response,
    ):
        assert response.status == 200
        assert int(response.headers["content-length"]) == RESPONSE_TOTAL
        hasher = hashlib.sha256()
        head = await response.content.readexactly(PREFIX)
        hasher.update(head)
        # The client holds PREFIX real bytes and the upstream has not begun its tail: not buffered.
        assert not up.tail_started.is_set()
        up.release.set()
        received = len(head)
        while block := await response.content.read(1 << 20):
            hasher.update(block)
            received += len(block)
    assert up.tail_started.is_set()
    assert received == RESPONSE_TOTAL
    assert hasher.hexdigest() == stream_digest(RESPONSE_TOTAL)


@dataclass
class BodyRecordingUpstream:
    """Reads a full request body and records its size, digest, and the (post-substitution) auth header."""

    port: int = 0
    authorization: str | None = None
    body_length: int = 0
    body_digest: str = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        self.authorization = headers.get("authorization")
        length = int(headers.get("content-length", "0"))
        hasher = hashlib.sha256()
        remaining = length
        while remaining > 0 and (block := await reader.read(min(1 << 20, remaining))):
            hasher.update(block)
            remaining -= len(block)
        self.body_length = length - remaining
        self.body_digest = hasher.hexdigest()
        body = b"stored"
        writer.write(f"HTTP/1.1 200 OK\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()


@asynccontextmanager
async def body_recording_upstream() -> AsyncIterator[BodyRecordingUpstream]:
    upstream = BodyRecordingUpstream()
    server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        server.close()
        await server.wait_closed()


async def test_large_request_body_forwarded_with_substitution(tmp_path: Path) -> None:
    """A large request body is forwarded intact and the header placeholder is still substituted.

    Requests take the buffered path (see module docstring), so the gate substitutes the header
    once the body has been read; the upstream must see the real credential and the exact bytes.
    """
    decide = StaticDecideClient(allow_with_substitution())
    payload = UNIT * (REQUEST_TOTAL // len(UNIT))
    async with (
        body_recording_upstream() as up,
        make_proxy(decide, tmp_path) as proxy,
        aiohttp.ClientSession() as session,
        session.post(
            f"http://127.0.0.1:{up.port}/upload",
            proxy=proxy_url(proxy),
            data=payload,
            headers={"Authorization": f"Bearer {PLACEHOLDER}"},
        ) as response,
    ):
        assert response.status == 200
    assert up.authorization == f"Bearer {REAL_CREDENTIAL}"
    assert up.body_length == REQUEST_TOTAL
    assert up.body_digest == stream_digest(REQUEST_TOTAL)


if __name__ == "__main__":
    pytest_bazel.main()
