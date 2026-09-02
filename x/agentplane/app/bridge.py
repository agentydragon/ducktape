"""The runner bridge: the runner protocol served to a browser as REST and SSE.

Nothing is reshaped. SSE payloads and command bodies are the proto-JSON encoding of the runner
protocol's own messages, so a browser reads the vocabulary of `protocol.proto` and the raw `Native`
frames pass through untouched. The bridge owns routing and framing only.

The runner takes one attachment per session, so the bridge holds one while any browser tab streams
the session and fans its events out to every tab: a tab opened later, or reconnecting from its last
event id, is served the history the attachment has read and then follows live. Commands go through
that attachment; when nobody is streaming, a command uses a short-lived attachment of its own. A
fresh Open would supersede the shared attachment, so it is refused while a stream is live.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import Attachment, RunnerClient, RunnerError, StreamClosedError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Seconds of silence after which the stream carries a comment, so proxies keep it open.
KEEPALIVE_S = 15

# Sandbox name to the `host:port` of its runner.
AddressOf = Callable[[str], Awaitable[str]]

# What a subscriber reads: the runner's events, then the exception that ended the attachment.
Inbox = asyncio.Queue[pb.Event | Exception]


class SandboxNotReachableError(Exception):
    """The sandbox has no running Pod to dial."""

    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class SessionStreamingError(Exception):
    """A browser is streaming the session, so a fresh Open would supersede it."""

    def __init__(self, sandbox: str, session_id: str) -> None:
        super().__init__(f"session {session_id!r} of sandbox {sandbox!r} is being streamed; detach it first")


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    spec: dict[str, object] = Field(description="Proto-JSON of the runner's SessionSpec.")


def runner_address(inventory: SandboxInventory, port: int) -> AddressOf:
    """Dial the sandbox's Pod directly; the address changes with every Pod, so it is resolved per use."""

    async def address_of(name: str) -> str:
        view = await inventory.get(name)
        if view.state is not ProvisioningState.RUNNING or view.pod is None or view.pod.ip is None:
            raise SandboxNotReachableError(name, view.state)
        return f"{view.pod.ip}:{port}"

    return address_of


class Feed:
    """The one attachment of a streamed session, read from the start of its log, and the tabs on it.

    The attachment's `seen` is the history a late subscriber replays from; the runner's whole log is
    held while the session is streamed, which is the size of one conversation."""

    def __init__(self, attachment: Attachment, name: str) -> None:
        self.attachment = attachment
        self.subscribers: set[Inbox] = set()
        self.ended: Exception | None = None
        self.reader = asyncio.create_task(self._pump(), name=name)

    async def _pump(self) -> None:
        try:
            while True:
                event = await self.attachment.next_event()
                for inbox in self.subscribers:
                    inbox.put_nowait(event)
        except Exception as error:  # the stream's end, delivered in order behind the events
            self.ended = error
            for inbox in self.subscribers:
                inbox.put_nowait(error)

    def subscribe(self, after_sequence: int) -> tuple[list[pb.Event], Inbox]:
        """The events already read past the cursor, and the queue the rest arrive on; taken in one
        step, so nothing falls between them."""
        inbox: Inbox = asyncio.Queue()
        self.subscribers.add(inbox)
        if self.ended is not None:
            inbox.put_nowait(self.ended)
        return [event for event in self.attachment.seen if event.sequence > after_sequence], inbox

    async def close(self) -> None:
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader
        self.attachment.cancel()


class RunnerBridge:
    def __init__(self, *, address_of: AddressOf) -> None:
        self._address_of = address_of
        self._clients: dict[str, RunnerClient] = {}
        self._feeds: dict[tuple[str, str], Feed] = {}
        # gRPC stream writes must not interleave: one command at a time per session, and a feed is
        # created or dropped under the same lock.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def _client(self, sandbox: str) -> RunnerClient:
        address = await self._address_of(sandbox)
        if address not in self._clients:
            self._clients[address] = RunnerClient(address)
        return self._clients[address]

    async def list_sessions(self, sandbox: str) -> list[pb.SessionSummary]:
        return await (await self._client(sandbox)).list_sessions()

    async def open_session(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
        """Create the session and start its harness; the browser's stream attaches afterwards."""
        key = (sandbox, session_id)
        async with self._lock(key):
            if key in self._feeds:
                raise SessionStreamingError(sandbox, session_id)
            attachment = await (await self._client(sandbox)).attach(session_id, spec=spec)
            await attachment.detach()
            await attachment.drain_until_end()
            return attachment.attached

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def send(self, sandbox: str, session_id: str, message: pb.Input) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.send(message.input_id, message.text))

    async def interrupt(self, sandbox: str, session_id: str) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.interrupt())

    async def shutdown(self, sandbox: str, session_id: str) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.shutdown(), ends_stream=True)

    async def _command(
        self,
        sandbox: str,
        session_id: str,
        command: Callable[[Attachment], Awaitable[None]],
        *,
        ends_stream: bool = False,
    ) -> None:
        key = (sandbox, session_id)
        async with self._lock(key):
            feed = self._feeds.get(key)
            if feed is not None:
                await command(feed.attachment)
                return
            attachment = await (await self._client(sandbox)).attach(session_id)
            await command(attachment)
            if not ends_stream:
                await attachment.detach()
            await attachment.drain_until_end()

    async def events(self, sandbox: str, session_id: str, *, after_sequence: int) -> AsyncIterator[bytes]:
        """The SSE stream: `attached`, then one `event` per runner event with its sequence as the SSE
        id, then `end` or `error`. A browser reconnecting with Last-Event-ID resumes without a gap."""
        key = (sandbox, session_id)
        async with self._lock(key):
            feed = self._feeds.get(key)
            if feed is None:
                feed = Feed(await (await self._client(sandbox)).attach(session_id), name=f"sse-{sandbox}-{session_id}")
                self._feeds[key] = feed
            replay, inbox = feed.subscribe(after_sequence)
        try:
            yield _frame("attached", MessageToDict(feed.attachment.attached))
            for event in replay:
                yield _frame("event", MessageToDict(event), event_id=event.sequence)
            while True:
                try:
                    item = await asyncio.wait_for(inbox.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                match item:
                    case pb.Event() if item.sequence <= after_sequence:
                        # A feed opened for this tab replays the runner's whole log; the cursor
                        # applies to those events too, not only to the ones already read.
                        continue
                    case pb.Event():
                        yield _frame("event", MessageToDict(item), event_id=item.sequence)
                    case StreamClosedError():
                        yield _frame("end", {})
                        return
                    case RunnerError():
                        yield _frame("error", {"message": str(item)})
                        return
                    case _:
                        raise item
        finally:
            feed.subscribers.discard(inbox)
            async with self._lock(key):
                if not feed.subscribers and self._feeds.get(key) is feed:
                    del self._feeds[key]
                    await feed.close()

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self._clients.values()))


def _frame(event: str, data: dict[str, object], *, event_id: int | None = None) -> bytes:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data)}")
    return ("\n".join(lines) + "\n\n").encode()


def _parse[M: pb.Input | pb.SessionSpec](message: M, body: dict[str, object]) -> M:
    try:
        return ParseDict(body, message)
    except ParseError as error:
        raise MalformedMessageError(f"not a {type(message).__name__}: {error}") from error


router = APIRouter(prefix="/sandboxes/{name}/sessions", tags=["sessions"])


def _bridge(request: Request) -> RunnerBridge:
    bridge = request.app.state.bridge
    if not isinstance(bridge, RunnerBridge):
        raise TypeError(f"app.state.bridge is {type(bridge).__name__}, not RunnerBridge")
    return bridge


Bridge = Annotated[RunnerBridge, Depends(_bridge)]


@router.get("")
async def list_sessions(bridge: Bridge, name: str) -> list[dict[str, object]]:
    return [MessageToDict(summary) for summary in await bridge.list_sessions(name)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def open_session(bridge: Bridge, name: str, body: NewSession) -> dict[str, object]:
    attached = await bridge.open_session(name, body.session_id, _parse(pb.SessionSpec(), body.spec))
    return MessageToDict(attached)


@router.get("/{session_id}/events")
async def session_events(
    bridge: Bridge,
    name: str,
    session_id: str,
    after: Annotated[int, Query(ge=0, description="Replay events with a greater sequence.")] = 0,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    # A browser's automatic reconnect sends the last id it saw; that wins over the query parameter.
    after_sequence = last_event_id if last_event_id is not None else after
    return StreamingResponse(
        bridge.events(name, session_id, after_sequence=after_sequence),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{session_id}/inputs", status_code=status.HTTP_202_ACCEPTED)
async def send_input(bridge: Bridge, name: str, session_id: str, body: dict[str, object]) -> Response:
    await bridge.send(name, session_id, _parse(pb.Input(), body))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{session_id}/interrupt", status_code=status.HTTP_202_ACCEPTED)
async def interrupt_session(bridge: Bridge, name: str, session_id: str) -> Response:
    await bridge.interrupt(name, session_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{session_id}/shutdown", status_code=status.HTTP_202_ACCEPTED)
async def shutdown_session(bridge: Bridge, name: str, session_id: str) -> Response:
    await bridge.shutdown(name, session_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
