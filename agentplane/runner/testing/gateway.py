"""What a runner dials, on one loopback port: a stand-in for the Runner Gateway's control listener that
tests call the runner's `Runner` service through, and an ingress that commits every Session's Events
for tests to read (agentplane/docs/runner_control.md)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc

from agentplane.grpc.serving import serving
from agentplane.grpc.tunnel import TunnelCaller
from agentplane.history_service import runner_ingress_pb2, runner_ingress_pb2_grpc
from agentplane.proto import event_log_pb2, grpc_tunnel_pb2
from agentplane.runner import runner_service_pb2_grpc
from agentplane.runner.configuration import Endpoint
from agentplane.runner_gateway import runner_control_pb2, runner_control_pb2_grpc

# gazelle:include_dep @pypi//protobuf

BEARER = "test-runner-bearer"


class ControlListener(runner_control_pb2_grpc.RunnerControlServicer):
    """Calls the newest runner connection's `Runner` service for tests, and records each connection.
    Scripted `closes` end the next connections right after `Welcome`, oldest first, and `silences`
    leave the next ones silent after it."""

    def __init__(self) -> None:
        # Connections opened so far; the newest is the one calls go to.
        self.connections = 0
        self.hellos: list[runner_control_pb2.Hello] = []
        self.peers: list[str] = []
        self.authorizations: list[str] = []
        self.closes: list[grpc.StatusCode] = []
        self.silences = 0
        # The facts each connection's heartbeats carried, in order.
        self.facts: list[runner_control_pb2.RunnerFacts] = []
        self.facts_published = asyncio.Condition()
        # The incarnation each new connection's Welcome names; a test changes it to start another.
        self.incarnation_uid = "test-incarnation"
        self._runner: runner_service_pb2_grpc.RunnerAsyncStub | None = None
        self._connected = asyncio.Condition()
        self._ready_connection = 0

    async def wait_connected(self, connections: int) -> None:
        """Until the runner holds a connection and has opened at least `connections` in all."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(lambda: self.connections >= connections and self._runner is not None)

    async def wait_ready(self, connections: int) -> None:
        """Until the newest connection publishes facts after adopting its incarnation."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(
                lambda: (
                    self.connections >= connections
                    and self._runner is not None
                    and self._ready_connection == self.connections
                )
            )

    async def wait_for_facts(self, expected: runner_control_pb2.RunnerFacts) -> None:
        """Until a heartbeat has carried `expected` in every fact but `idle_since`."""

        def carried(facts: runner_control_pb2.RunnerFacts) -> bool:
            without_idle = runner_control_pb2.RunnerFacts()
            without_idle.CopyFrom(facts)
            without_idle.ClearField("idle_since")
            return without_idle == expected

        async with asyncio.timeout(30), self.facts_published:
            await self.facts_published.wait_for(lambda: any(carried(facts) for facts in self.facts))

    async def wait_for_idleness(self, *, idle: bool) -> None:
        """Until the newest heartbeat says the Sandbox is idle (`idle_since` set) or busy (absent)."""
        async with asyncio.timeout(30), self.facts_published:
            await self.facts_published.wait_for(
                lambda: bool(self.facts) and self.facts[-1].HasField("idle_since") == idle
            )

    async def runner(self) -> runner_service_pb2_grpc.RunnerAsyncStub:
        """The runner's service through its newest connection, after incarnation adoption."""
        await self.wait_ready(1)
        assert self._runner is not None
        return self._runner

    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_control_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        self.authorizations += [
            str(value) for key, value in context.invocation_metadata() or () if key == "authorization"
        ]
        self.hellos.append((await anext(request_iterator)).hello)
        self.peers.append(context.peer())
        await context.write(
            runner_control_pb2.ServiceFrame(
                welcome=runner_control_pb2.Welcome(
                    connection_epoch=len(self.hellos), incarnation_uid=self.incarnation_uid
                )
            )
        )
        if self.closes:
            await context.abort(self.closes.pop(0), "test service closes this connection")
        if self.silences:
            self.silences -= 1
            await asyncio.Event().wait()
        writing = asyncio.Lock()

        async def send(frame: runner_control_pb2.ServiceFrame) -> None:
            async with writing:
                await context.write(frame)

        async def call(tunneled: grpc_tunnel_pb2.Call) -> None:
            await send(runner_control_pb2.ServiceFrame(call=tunneled))

        async with TunnelCaller(call) as tunnel:
            runner = runner_service_pb2_grpc.RunnerStub(tunnel.channel)
            async with self._connected:
                self.connections += 1
                self._runner = runner
                self._connected.notify_all()
            try:
                async for frame in request_iterator:
                    match frame.WhichOneof("frame"):
                        case "heartbeat":
                            if frame.heartbeat.HasField("facts"):
                                async with self.facts_published:
                                    self.facts.append(frame.heartbeat.facts)
                                    self.facts_published.notify_all()
                                async with self._connected:
                                    if self._runner is runner:
                                        self._ready_connection = self.connections
                                        self._connected.notify_all()
                            await send(runner_control_pb2.ServiceFrame(heartbeat=runner_control_pb2.Heartbeat()))
                        case "reply":
                            tunnel.answer(frame.reply)
            finally:
                if self._runner is runner:
                    self._runner = None
                    self._ready_connection = 0


class IngressStore(runner_ingress_pb2_grpc.RunnerIngressServicer):
    """Observes ingress commits, optionally forwarding the runner stream to History."""

    def __init__(self) -> None:
        self.committed: dict[str, list[event_log_pb2.EventEntry]] = {}
        self.sealed: dict[str, int] = {}
        self._sealing_incarnation: dict[str, str] = {}
        self._cursor: dict[str, int] = {}
        # Forwarded entries History has not acknowledged yet. They outlive their Push stream: a runner
        # that exits right after sending learns of History's commit only from its next stream's resume
        # cursor, and does not send the entry again.
        self._pending: dict[str, dict[int, event_log_pb2.EventEntry]] = {}
        self._changed = asyncio.Condition()
        self._forward_target: tuple[str, str] | None = None
        self._forward_pause_lock = asyncio.Lock()
        self._forward_pause_count = 0
        self._forward_resumed = asyncio.Event()
        self._forward_resumed.set()
        self._route_changed = asyncio.Event()

    def forward_to(self, target: str, bearer: str) -> None:
        """Forward future Push streams to a real ingress and relay its acknowledgements."""
        if not target or not bearer:
            raise ValueError("an ingress target and bearer are required")
        # Local acknowledgements are not History commits. Start observer cursors from scratch;
        # the runner reconnects and the real ingress supplies the authoritative resume cursor.
        self.committed.clear()
        self.sealed.clear()
        self._sealing_incarnation.clear()
        self._cursor.clear()
        self._pending.clear()
        self._forward_target = (target, bearer)
        self._route_changed.set()

    def stop_forwarding(self) -> None:
        self._forward_target = None
        self._route_changed.set()

    @asynccontextmanager
    async def forwarding_paused(self) -> AsyncIterator[None]:
        """Hold entry and seal frames before History while the independent control stream runs."""
        async with self._forward_pause_lock:
            self._forward_pause_count += 1
            self._forward_resumed.clear()
        try:
            yield
        finally:
            async with self._forward_pause_lock:
                self._forward_pause_count -= 1
                if self._forward_pause_count == 0:
                    self._forward_resumed.set()

    async def _wait_forwarding_resumed(self) -> None:
        while True:
            async with self._forward_pause_lock:
                if self._forward_pause_count == 0:
                    return
                resumed = self._forward_resumed
            await resumed.wait()

    def since(self, session_id: str, cursor: int) -> list[event_log_pb2.EventEntry]:
        return [entry for entry in self.committed.get(session_id, []) if entry.cursor > cursor]

    async def wait_beyond(self, session_id: str, cursor: int) -> None:
        """Until an entry follows `cursor`, or the Session's seal is committed."""
        async with self._changed:
            await self._changed.wait_for(lambda: self._cursor.get(session_id, 0) > cursor or session_id in self.sealed)

    async def wait_sealed(self, session_id: str) -> None:
        async with asyncio.timeout(30), self._changed:
            await self._changed.wait_for(lambda: session_id in self.sealed)

    @override
    async def Push(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
    ) -> None:
        first = await anext(request_iterator)
        announce = first.announce
        if announce is None:
            raise ValueError("the first ingress frame must be Announce")
        session_id = announce.session_id
        # A continuation under another incarnation replaces the seal, as the History Service does.
        if (
            announce.HasField("incarnation_uid")
            and self._sealing_incarnation.get(session_id, announce.incarnation_uid) != announce.incarnation_uid
        ):
            del self.sealed[session_id]
        committed = self.committed.setdefault(session_id, [])
        self._cursor.setdefault(session_id, max((entry.cursor for entry in committed), default=0))

        if self._forward_target is not None:
            incarnation_uid = announce.incarnation_uid if announce.HasField("incarnation_uid") else None
            await self._forward(first, request_iterator, context, session_id, incarnation_uid)
            return

        async def acknowledge() -> None:
            await context.write(
                runner_ingress_pb2.Acknowledgement(cursor=self._cursor[session_id], sealed=session_id in self.sealed)
            )

        await acknowledge()
        frames = request_iterator.__aiter__()
        while True:
            if self._forward_target is not None:
                # The runner will reconnect and this next call will be proxied to History.
                return
            self._route_changed.clear()
            next_frame = asyncio.ensure_future(anext(frames))
            route_change = asyncio.create_task(self._route_changed.wait())
            try:
                done, _ = await asyncio.wait((next_frame, route_change), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (next_frame, route_change):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(next_frame, route_change, return_exceptions=True)
            if route_change in done and self._forward_target is not None:
                return
            if next_frame not in done:
                continue
            try:
                frame = await next_frame
            except StopAsyncIteration:
                return
            match frame.WhichOneof("frame"):
                case "entry" if frame.entry.cursor > self._cursor[session_id]:
                    if frame.entry.cursor != self._cursor[session_id] + 1:
                        raise ValueError(
                            f"session {session_id}: {frame.entry.cursor=} skips past {self._cursor[session_id]=}"
                        )
                    committed.append(frame.entry)
                    self._cursor[session_id] = frame.entry.cursor
                case "sealed":
                    self.sealed[session_id] = frame.sealed.cursor
                    if frame.sealed.HasField("incarnation_uid"):
                        self._sealing_incarnation[session_id] = frame.sealed.incarnation_uid
            await acknowledge()
            async with self._changed:
                self._changed.notify_all()

    async def _forward(
        self,
        first: runner_ingress_pb2.PushFrame,
        request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame],
        context: grpc.aio.ServicerContext,
        session_id: str,
        incarnation_uid: str | None,
    ) -> None:
        target = self._forward_target
        if target is None:
            raise RuntimeError("History ingress forwarding was disabled before the stream started")
        address, bearer = target
        pending = self._pending.setdefault(session_id, {})
        pending_seal: runner_ingress_pb2.PushFrame | None = None

        async def frames() -> AsyncIterator[runner_ingress_pb2.PushFrame]:
            nonlocal pending_seal
            yield first
            async for frame in request_iterator:
                match frame.WhichOneof("frame"):
                    case "entry":
                        await self._wait_forwarding_resumed()
                        entry = event_log_pb2.EventEntry()
                        entry.CopyFrom(frame.entry)
                        pending[entry.cursor] = entry
                    case "sealed":
                        await self._wait_forwarding_resumed()
                        pending_seal = runner_ingress_pb2.PushFrame()
                        pending_seal.CopyFrom(frame)
                    case _:
                        pass
                yield frame

        try:
            async with grpc.aio.insecure_channel(address) as channel:
                call = runner_ingress_pb2_grpc.RunnerIngressStub(channel).Push(
                    frames(), metadata=(("authorization", f"Bearer {bearer}"),)
                )
                async for acknowledgement in call:
                    previous = self._cursor.get(session_id, 0)
                    if acknowledgement.cursor < previous:
                        raise ValueError(
                            f"session {session_id}: upstream acknowledgement "
                            f"{acknowledgement.cursor} moved behind {previous}"
                        )
                    committed = self.committed.setdefault(session_id, [])
                    for cursor in sorted(cursor for cursor in pending if previous < cursor <= acknowledgement.cursor):
                        committed.append(pending.pop(cursor))
                    for cursor in tuple(pending):
                        if cursor <= acknowledgement.cursor:
                            pending.pop(cursor)
                    self._cursor[session_id] = acknowledgement.cursor
                    if acknowledgement.sealed:
                        self.sealed[session_id] = acknowledgement.cursor
                        if pending_seal is not None and pending_seal.sealed.HasField("incarnation_uid"):
                            self._sealing_incarnation[session_id] = pending_seal.sealed.incarnation_uid
                        elif incarnation_uid is not None:
                            self._sealing_incarnation[session_id] = incarnation_uid
                    await context.write(acknowledgement)
                    async with self._changed:
                        self._changed.notify_all()
        except grpc.aio.AioRpcError as error:
            await context.abort(error.code(), error.details() or "")


@dataclass(frozen=True)
class Gateway:
    control: ControlListener
    ingress: IngressStore
    endpoint: Endpoint


@asynccontextmanager
async def serve_gateway() -> AsyncIterator[Gateway]:
    control, ingress = ControlListener(), IngressStore()
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(control, server)
    runner_ingress_pb2_grpc.add_RunnerIngressServicer_to_server(ingress, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Gateway(control, ingress, Endpoint(target=f"127.0.0.1:{port}", bearer=BEARER))
