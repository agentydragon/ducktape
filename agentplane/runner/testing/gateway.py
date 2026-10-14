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

from agentplane.background_task import background
from agentplane.grpc_serving import serving
from agentplane.grpc_tunnel import TunnelCaller
from agentplane.proto import (
    event_log_pb2,
    grpc_tunnel_pb2,
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_ingress_pb2,
    runner_ingress_pb2_grpc,
)
from agentplane.runner.config import ServiceEndpoint
from agentplane.runner.control import RunnerControlClient, RunnerService
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.reconnect import StreamTiming
from agentplane.runner.runner import Runner

# gazelle:include_dep @pypi//protobuf

BEARER = "test-runner-bearer"
# Prompt enough that an in-process runner connects at once and survives a test's idle stretches.
TIMING = StreamTiming(heartbeat_s=0.1, liveness_s=5, backoff_initial_s=0.05, backoff_max_s=0.5)


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
        # Every RunnerFacts frame received, oldest first.
        self.facts: list[runner_control_pb2.RunnerFacts] = []
        # The incarnation each new connection's Welcome names; a test changes it to start another.
        self.incarnation_uid = "test-incarnation"
        self._runner: runner_control_pb2_grpc.RunnerAsyncStub | None = None
        self._connected = asyncio.Condition()

    async def wait_connected(self, connections: int) -> None:
        """Until the runner holds a connection and has opened at least `connections` in all."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(lambda: self.connections >= connections and self._runner is not None)

    async def wait_bootstrap(self, bootstrap: runner_control_pb2.RunnerFacts.Bootstrap) -> None:
        """Until the newest facts the runner sent report `bootstrap`."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(lambda: bool(self.facts) and self.facts[-1].bootstrap == bootstrap)

    async def runner(self) -> runner_control_pb2_grpc.RunnerAsyncStub:
        """The runner's service through its newest connection, once it has one."""
        await self.wait_connected(1)
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
            runner = runner_control_pb2_grpc.RunnerStub(tunnel.channel)
            async with self._connected:
                self.connections += 1
                self._runner = runner
                self._connected.notify_all()
            try:
                async for frame in request_iterator:
                    match frame.WhichOneof("frame"):
                        case "heartbeat":
                            await send(runner_control_pb2.ServiceFrame(heartbeat=runner_control_pb2.Heartbeat()))
                        case "reply":
                            tunnel.answer(frame.reply)
                        case "facts":
                            async with self._connected:
                                self.facts.append(frame.facts)
                                self._connected.notify_all()
            finally:
                if self._runner is runner:
                    self._runner = None


class IngressStore(runner_ingress_pb2_grpc.RunnerIngressServicer):
    """Commits each Session's entries as they arrive and acknowledges every frame."""

    def __init__(self) -> None:
        self.committed: dict[str, list[event_log_pb2.EventEntry]] = {}
        self.sealed: dict[str, int] = {}
        self._changed = asyncio.Condition()

    def since(self, session_id: str, cursor: int) -> list[event_log_pb2.EventEntry]:
        return self.committed.get(session_id, [])[cursor:]

    async def wait_beyond(self, session_id: str, cursor: int) -> None:
        """Until an entry follows `cursor`, or the Session's seal is committed."""
        async with self._changed:
            await self._changed.wait_for(
                lambda: len(self.committed.get(session_id, [])) > cursor or session_id in self.sealed
            )

    async def wait_sealed(self, session_id: str) -> None:
        async with asyncio.timeout(30), self._changed:
            await self._changed.wait_for(lambda: session_id in self.sealed)

    @override
    async def Push(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
    ) -> None:
        session_id = (await anext(request_iterator)).announce.session_id
        committed = self.committed.setdefault(session_id, [])

        async def acknowledge() -> None:
            await context.write(
                runner_ingress_pb2.Acknowledgement(cursor=len(committed), sealed=session_id in self.sealed)
            )

        await acknowledge()
        async for frame in request_iterator:
            match frame.WhichOneof("frame"):
                case "entry" if frame.entry.cursor > len(committed):
                    if frame.entry.cursor != len(committed) + 1:
                        raise ValueError(f"session {session_id}: {frame.entry.cursor=} skips past {len(committed)=}")
                    committed.append(frame.entry)
                case "sealed":
                    self.sealed[session_id] = frame.sealed.cursor
            await acknowledge()
            async with self._changed:
                self._changed.notify_all()


@dataclass(frozen=True)
class Gateway:
    control: ControlListener
    ingress: IngressStore
    endpoint: ServiceEndpoint


@asynccontextmanager
async def serve_gateway() -> AsyncIterator[Gateway]:
    control, ingress = ControlListener(), IngressStore()
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(control, server)
    runner_ingress_pb2_grpc.add_RunnerIngressServicer_to_server(ingress, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Gateway(control, ingress, ServiceEndpoint(target=f"127.0.0.1:{port}", bearer=BEARER))


@asynccontextmanager
async def dialing(runner: Runner, gateway: Gateway) -> AsyncIterator[None]:
    """`runner` keeps its control stream to `gateway` and pushes its Events there, as the runner process
    does with `--control-target` and `--ingress-target`. Requests go to it once the block starts: a
    previous runner's connection may not have been seen to end yet."""
    target, bearer = gateway.endpoint.target, gateway.endpoint.bearer
    connections = gateway.control.connections
    async with (
        background(
            RunnerControlClient(
                RunnerService(runner), target=target, bearer=bearer, timing=TIMING, sandbox=runner
            ).run(),
            name="runner control stream",
        ),
        background(IngressPusher(runner, target=target, bearer=bearer, timing=TIMING).run(), name="ingress push"),
    ):
        await gateway.control.wait_connected(connections + 1)
        yield
