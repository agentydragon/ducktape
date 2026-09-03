"""The runner bridge: the runner protocol served to a browser as REST and SSE, and every event
copied into the trajectory store as it arrives.

Nothing is reshaped. SSE payloads and command bodies are the proto-JSON encoding of the runner
protocol's own messages, so a browser reads the vocabulary of `protocol.proto` and the raw `Native`
frames pass through untouched. The bridge owns routing and framing only.

The runner takes one attachment per session, so the bridge holds one per session it follows: a feed
that records each event to the store and fans it out to every browser tab on the session. A tab
opened later, or reconnecting from its last event id, reads the stored history first and then
follows live, so the runner is asked for the events since the last one stored, never the whole log.
A feed starts when the app opens a session, when a tab asks for one, and at startup for every
session whose harness is running; it ends when the runner ends the stream, or when the sandbox is
gone. Commands go through the feed's attachment; without a feed, a command uses a short-lived
attachment of its own. A fresh Open would supersede the feed's attachment, so it is refused while a
feed is live.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

import grpc
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, retry_if_exception_type, wait_exponential

from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import Attachment, RunnerClient, RunnerError, StreamClosedError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
# Stored history streams to a tab in pages of this many events, so a long thread replays whole.
REPLAY_PAGE = 1000

# Seconds of silence after which the stream carries a comment, so proxies keep it open.
KEEPALIVE_S = 15

# Sandbox name to the `host:port` of its runner.
AddressOf = Callable[[str], Awaitable[str]]

# What a subscriber reads: the runner's events, then the exception that ended the feed.
Inbox = asyncio.Queue[pb.Event | Exception]


class SandboxNotReachableError(Exception):
    """The sandbox has no running Pod to dial."""

    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class SessionStreamingError(Exception):
    """The bridge follows the session, so a fresh Open would supersede its attachment."""

    def __init__(self, sandbox: str, session_id: str) -> None:
        super().__init__(f"session {session_id!r} of sandbox {sandbox!r} is being followed; shut it down first")


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
    """The bridge's attachment to one session: records to the store, fans out to the tabs.

    `attach` connects once, so a caller learns whether the runner is reachable; `run` then reads
    until the runner ends the stream, reconnecting from the last stored sequence when the
    connection drops (a replaced Pod, a runner restart), and ends the feed when the sandbox is
    gone or the runner ends the stream."""

    def __init__(
        self, *, key: tuple[str, str], client_for: Callable[[], Awaitable[RunnerClient]], store: TrajectoryStore
    ):
        self.key = key
        self._client_for = client_for
        self._store = store
        self.attachment: Attachment | None = None
        self.attached: pb.Attached | None = None
        self.thread_id: UUID | None = None
        self.subscribers: set[Inbox] = set()
        self.ended: Exception | None = None
        self.task: asyncio.Task[None] | None = None

    async def attach(self) -> None:
        sandbox, session_id = self.key
        if self.thread_id is None:
            attachment = await (await self._client_for()).attach(session_id)
            self.thread_id = await self._store.thread(sandbox, session_id, attachment.attached.spec)
            stored = await self._store.last_sequence(self.thread_id)
            if stored < attachment.attached.last_sequence or stored == 0:
                # The whole log replays; what is already stored is skipped by the store itself.
                self.attachment = attachment
            else:
                # Everything is stored: reattach after it, so the runner sends live events only.
                attachment.cancel()
                self.attachment = await self._reattach(session_id, stored)
        else:
            self.attachment = await self._reattach(session_id, await self._store.last_sequence(self.thread_id))
        self.attached = self.attachment.attached

    async def _reattach(self, session_id: str, stored: int) -> Attachment:
        """Attach after the stored cursor, or after the runner's whole log where that is shorter: a
        runner whose log restarted (a recreated sandbox reusing the session id) replays from its
        start, and the store keeps what it already holds under those sequences."""
        probe = await (await self._client_for()).attach(session_id)
        if stored <= probe.attached.last_sequence:
            probe.cancel()
            return await (await self._client_for()).attach(session_id, after_sequence=stored)
        logger.warning(
            "feed %s: the store holds sequences up to %d but the runner's log ends at %d; replaying its log",
            self.key,
            stored,
            probe.attached.last_sequence,
        )
        return probe

    async def run(self) -> None:
        assert self.attachment is not None
        assert self.thread_id is not None
        try:
            while True:
                try:
                    await self._pump(self.attachment, self.thread_id)
                    return
                except (grpc.aio.AioRpcError, ConnectionError) as error:
                    logger.warning("feed %s lost its connection: %s; reconnecting", self.key, error)
                    self.attachment.cancel()
                async for attempt in AsyncRetrying(
                    wait=wait_exponential(min=1, max=30),
                    retry=retry_if_exception_type((grpc.aio.AioRpcError, ConnectionError)),
                ):
                    with attempt:
                        await self.attach()
        except (StreamClosedError, RunnerError, SandboxNotReachableError) as error:
            self._end(error)
        except asyncio.CancelledError:
            self._end(StreamClosedError())
            raise
        except Exception as error:
            logger.exception("feed %s failed", self.key)
            self._end(error)
        finally:
            if self.attachment is not None:
                self.attachment.cancel()

    async def _pump(self, attachment: Attachment, thread_id: UUID) -> None:
        while True:
            event = await attachment.next_event()
            await self._store.record(thread_id, [event])
            for inbox in self.subscribers:
                inbox.put_nowait(event)

    def _end(self, error: Exception) -> None:
        self.ended = error
        for inbox in self.subscribers:
            inbox.put_nowait(error)

    def subscribe(self) -> Inbox:
        inbox: Inbox = asyncio.Queue()
        self.subscribers.add(inbox)
        if self.ended is not None:
            inbox.put_nowait(self.ended)
        return inbox

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class RunnerBridge:
    def __init__(self, *, address_of: AddressOf, store: TrajectoryStore) -> None:
        self._address_of = address_of
        self._store = store
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

    async def start(self, running_sandboxes: list[str]) -> None:
        """Follow every session with a running harness, so recording does not wait for a tab."""
        for sandbox in running_sandboxes:
            try:
                summaries = await self.list_sessions(sandbox)
            except (grpc.aio.AioRpcError, SandboxNotReachableError) as error:
                logger.warning("sandbox %s not followed at startup: %s", sandbox, error)
                continue
            for summary in summaries:
                if summary.harness == pb.HARNESS_STATE_RUNNING:
                    await self._ensure_feed((sandbox, summary.session_id))

    async def list_sessions(self, sandbox: str) -> list[pb.SessionSummary]:
        return await (await self._client(sandbox)).list_sessions()

    async def open_session(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
        """Create the session and start its harness, then follow it."""
        key = (sandbox, session_id)
        async with self._lock(key):
            if self._live_feed(key) is not None:
                raise SessionStreamingError(sandbox, session_id)
            attachment = await (await self._client(sandbox)).attach(session_id, spec=spec)
            await attachment.detach()
            await attachment.drain_until_end()
        await self._ensure_feed(key)
        return attachment.attached

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _live_feed(self, key: tuple[str, str]) -> Feed | None:
        feed = self._feeds.get(key)
        if feed is None or feed.ended is not None:
            return None
        return feed

    async def _ensure_feed(self, key: tuple[str, str]) -> Feed:
        async with self._lock(key):
            feed = self._live_feed(key)
            if feed is not None:
                return feed
            sandbox, _ = key
            feed = Feed(key=key, client_for=lambda: self._client(sandbox), store=self._store)
            await feed.attach()
            feed.task = asyncio.create_task(feed.run(), name=f"feed-{sandbox}-{key[1]}")
            self._feeds[key] = feed
            return feed

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
            feed = self._live_feed(key)
            if feed is not None and feed.attachment is not None:
                await command(feed.attachment)
                if ends_stream and feed.task is not None:
                    # Shutdown answers once the harness has stopped and the stream has ended, so a
                    # caller that lists sessions next sees the harness stopped.
                    await feed.task
                return
            attachment = await (await self._client(sandbox)).attach(session_id)
            await command(attachment)
            if not ends_stream:
                await attachment.detach()
            await attachment.drain_until_end()

    async def events(self, sandbox: str, session_id: str, *, after_sequence: int) -> AsyncIterator[bytes]:
        """The SSE stream: `attached`, then one `event` per runner event with its sequence as the SSE
        id, then `end` or `error`. History comes from the store, then the feed's live events; a
        browser reconnecting with Last-Event-ID resumes without a gap."""
        feed = await self._ensure_feed((sandbox, session_id))
        assert feed.attached is not None
        assert feed.thread_id is not None
        inbox = feed.subscribe()
        try:
            yield _frame("attached", MessageToDict(feed.attached))
            cursor = after_sequence
            while page := await self._store.events(feed.thread_id, after_sequence=cursor, limit=REPLAY_PAGE):
                for event in page:
                    yield _frame("event", MessageToDict(event), event_id=event.sequence)
                    cursor = event.sequence
            while True:
                try:
                    item = await asyncio.wait_for(inbox.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                match item:
                    case pb.Event():
                        # Queued before the stored history was read, and already in it.
                        if item.sequence <= cursor:
                            continue
                        yield _frame("event", MessageToDict(item), event_id=item.sequence)
                        cursor = item.sequence
                    case StreamClosedError():
                        yield _frame("end", {})
                        return
                    case RunnerError() | SandboxNotReachableError():
                        yield _frame("error", {"message": str(item)})
                        return
                    case _:
                        raise item
        finally:
            feed.subscribers.discard(inbox)

    async def close(self) -> None:
        await asyncio.gather(*(feed.close() for feed in self._feeds.values()))
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
