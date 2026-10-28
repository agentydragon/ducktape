"""A scriptable RunnerControl peer for tests of runner tunnel reconnect and failure behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc

from agentplane.grpc.serving import serving
from agentplane.grpc.tunnel import TunnelCaller
from agentplane.proto import grpc_tunnel_pb2
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


@dataclass(frozen=True)
class Gateway:
    """A Runner control peer whose stream faults tests can script explicitly."""

    control: ControlListener
    endpoint: Endpoint


@asynccontextmanager
async def serve_gateway() -> AsyncIterator[Gateway]:
    control = ControlListener()
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(control, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Gateway(control, Endpoint(target=f"127.0.0.1:{port}", bearer=BEARER))
