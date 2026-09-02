"""The runner bridge: the runner protocol served to a browser as REST and SSE.

Nothing is reshaped. SSE payloads and command bodies are the proto-JSON encoding of the runner
protocol's own messages, so a browser reads the vocabulary of `protocol.proto` and the raw `Native`
frames pass through untouched. The bridge owns routing and framing only.

One live attachment per session is kept while a browser streams it, and commands go through that
attachment; when nobody is streaming, a command uses a short-lived attachment of its own. A fresh
Open would otherwise supersede the browser's stream.
"""

from __future__ import annotations

import asyncio
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


class SandboxNotReachableError(Exception):
    """The sandbox has no running Pod to dial."""

    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    spec: dict[str, object] = Field(description="Proto-JSON of the runner's SessionSpec.")


def runner_address(inventory: SandboxInventory, port: int) -> AddressOf:
    """Dial the sandbox's Pod directly; the address changes with every Pod, so it is resolved per use."""

    async def address_of(name: str) -> str:
        view = await inventory.get(name)
        if view.state is not ProvisioningState.RUNNING or view.pod_ip is None:
            raise SandboxNotReachableError(name, view.state)
        return f"{view.pod_ip}:{port}"

    return address_of


class RunnerBridge:
    def __init__(self, *, address_of: AddressOf) -> None:
        self._address_of = address_of
        self._clients: dict[str, RunnerClient] = {}
        self._live: dict[tuple[str, str], Attachment] = {}

    async def _client(self, sandbox: str) -> RunnerClient:
        address = await self._address_of(sandbox)
        if address not in self._clients:
            self._clients[address] = RunnerClient(address)
        return self._clients[address]

    async def list_sessions(self, sandbox: str) -> list[pb.SessionSummary]:
        return await (await self._client(sandbox)).list_sessions()

    async def open_session(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
        """Create the session and start its harness; the browser's stream attaches afterwards."""
        attachment = await (await self._client(sandbox)).attach(session_id, spec=spec)
        await attachment.detach()
        await attachment.drain_until_end()
        return attachment.attached

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
        live = self._live.get((sandbox, session_id))
        if live is not None:
            await command(live)
            return
        attachment = await (await self._client(sandbox)).attach(session_id)
        await command(attachment)
        if not ends_stream:
            await attachment.detach()
        await attachment.drain_until_end()

    async def events(self, sandbox: str, session_id: str, *, after_sequence: int) -> AsyncIterator[bytes]:
        """The SSE stream: `attached`, then one `event` per runner event with its sequence as the SSE
        id, then `end` or `error`. A browser reconnecting with Last-Event-ID resumes without a gap."""
        attachment = await (await self._client(sandbox)).attach(session_id, after_sequence=after_sequence)
        key = (sandbox, session_id)
        self._live[key] = attachment
        inbox: asyncio.Queue[pb.Event | Exception] = asyncio.Queue()

        async def pump() -> None:
            try:
                while True:
                    inbox.put_nowait(await attachment.next_event())
            except Exception as error:  # the stream's end, delivered in order behind the events
                inbox.put_nowait(error)

        reader = asyncio.create_task(pump(), name=f"sse-{sandbox}-{session_id}")
        try:
            yield _frame("attached", MessageToDict(attachment.attached))
            while True:
                try:
                    item = await asyncio.wait_for(inbox.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                match item:
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
            if self._live.get(key) is attachment:
                del self._live[key]
            reader.cancel()
            attachment.cancel()

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
