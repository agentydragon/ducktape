"""Server tests over a real loopback socket, so framing and connection handling are exercised.

The adapter is a fake: what the console decides is not this module's concern, but *that a decision
becomes the right bytes*, that a pipelined connection keeps working, and that a failing adapter
drops the connection rather than letting the request through, all are.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
import pytest_bazel

from haku.console.icap_protocol import (
    Adaptation,
    Forward,
    Headers,
    HttpMessage,
    Modify,
    OptionsAnnouncement,
    ReqmodRequest,
    Respond,
)
from haku.console.icap_server import IcapServer

ISTAG = "console-test-1"

REQMOD_HEAD = (
    b"REQMOD icap://console/reqmod ICAP/1.0\r\n"
    b"Host: console\r\n"
    b"Allow: 204\r\n"
    b"X-Client-IP: 10.244.8.246\r\n"
    b"Encapsulated: req-hdr=0, null-body=104\r\n"
    b"\r\n"
    b"GET /v1/messages HTTP/1.1\r\n"
    b"Host: api.anthropic.com\r\n"
    b"Authorization: Bearer placeholder-token\r\n"
    b"\r\n"
)


@dataclass
class FakeAdapter:
    """Returns a scripted adaptation and records what it was asked about."""

    adaptation: Adaptation = field(default_factory=Forward)
    error: Exception | None = None
    seen: list[ReqmodRequest] = field(default_factory=list)

    async def adapt(self, request: ReqmodRequest) -> Adaptation:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        return self.adaptation

    @property
    def announcement(self) -> OptionsAnnouncement:
        return OptionsAnnouncement(istag=ISTAG, service="haku-console-test")


@dataclass
class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def send(self, payload: bytes) -> None:
        self.writer.write(payload)
        await self.writer.drain()

    async def status_line(self) -> bytes:
        return (await self.reader.readline()).strip()

    async def drain_head(self) -> bytes:
        """Read to the end of the ICAP header block, returning it."""
        lines = []
        while (line := await self.reader.readline()) not in (b"\r\n", b""):
            lines.append(line)
        return b"".join(lines)


@pytest.fixture
async def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
async def client(adapter: FakeAdapter):
    server = IcapServer(adapter, host="127.0.0.1", port=0)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    yield Client(reader, writer)
    writer.close()
    await server.stop()


async def test_options_answers_with_the_adapter_announcement(client: Client):
    await client.send(b"OPTIONS icap://console/reqmod ICAP/1.0\r\nHost: console\r\n\r\n")

    assert await client.status_line() == b"ICAP/1.0 200 OK"
    head = await client.drain_head()
    assert b"Methods: REQMOD" in head
    assert b'ISTag: "console-test-1"' in head


async def test_forward_becomes_204(client: Client, adapter: FakeAdapter):
    await client.send(REQMOD_HEAD)

    assert await client.status_line() == b"ICAP/1.0 204 No Content"
    assert adapter.seen[0].client_ip == "10.244.8.246"


async def test_modify_puts_the_substituted_credential_on_the_wire(client: Client, adapter: FakeAdapter):
    original = HttpMessage(
        b"GET /v1/messages HTTP/1.1",
        Headers((("Host", "api.anthropic.com"), ("Authorization", "Bearer placeholder-token"))),
    )
    adapter.adaptation = Modify(
        HttpMessage(original.start_line, original.headers.replacing("Authorization", "Bearer real-secret"))
    )

    await client.send(REQMOD_HEAD)

    assert await client.status_line() == b"ICAP/1.0 200 OK"
    body = await client.reader.readuntil(b"\r\n\r\n") + await client.reader.readuntil(b"\r\n\r\n")
    assert b"Authorization: Bearer real-secret" in body
    assert b"placeholder-token" not in body


async def test_respond_blocks_without_reaching_the_origin(client: Client, adapter: FakeAdapter):
    adapter.adaptation = Respond(b"HTTP/1.1 403 Forbidden", Headers((("Content-Length", "7"),)), b"denied\n")

    await client.send(REQMOD_HEAD)

    assert await client.status_line() == b"ICAP/1.0 200 OK"
    head = await client.drain_head()
    assert b"Encapsulated: res-hdr=0, res-body=" in head


async def test_connection_carries_more_than_one_transaction(client: Client, adapter: FakeAdapter):
    await client.send(REQMOD_HEAD)
    assert await client.status_line() == b"ICAP/1.0 204 No Content"
    await client.drain_head()

    await client.send(REQMOD_HEAD)
    assert await client.status_line() == b"ICAP/1.0 204 No Content"
    assert len(adapter.seen) == 2


async def test_a_failing_adapter_drops_the_connection(client: Client, adapter: FakeAdapter):
    adapter.error = RuntimeError("policy backend unreachable")

    await client.send(REQMOD_HEAD)

    # No ICAP response at all. With bypass=0 Squid turns that into ERR_ICAP_FAILURE and denies the
    # request; answering anything else here would forward the agent's unresolved placeholder.
    assert await client.reader.read() == b""


async def test_a_malformed_request_drops_the_connection(client: Client):
    await client.send(b"REQMOD icap://console/reqmod ICAP/1.0\r\nHost: console\r\n\r\n")

    assert await client.reader.read() == b""


async def test_preview_is_completed_before_the_adapter_is_asked(client: Client, adapter: FakeAdapter):
    await client.send(
        b"REQMOD icap://console/reqmod ICAP/1.0\r\n"
        b"Host: console\r\n"
        b"Allow: 204\r\n"
        b"Preview: 4\r\n"
        b"Encapsulated: req-hdr=0, req-body=60\r\n"
        b"\r\n"
        b"POST /v1/messages HTTP/1.1\r\n"
        b"Host: api.anthropic.com\r\n"
        b"\r\n"
        b"4\r\nabcd\r\n0\r\n\r\n"
    )

    assert await client.status_line() == b"ICAP/1.0 100 Continue"
    await client.drain_head()
    await client.send(b"3\r\nefg\r\n0\r\n\r\n")

    assert await client.status_line() == b"ICAP/1.0 204 No Content"
    # The adapter rules on the whole body, never on a fragment of it.
    assert adapter.seen[0].http.body == b"abcdefg"


if __name__ == "__main__":
    pytest_bazel.main()
