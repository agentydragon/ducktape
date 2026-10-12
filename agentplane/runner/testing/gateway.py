"""What a runner dials, on one loopback port: a stand-in for the Runner Gateway's control listener that
tests send lifecycle requests and commands through, and an ingress that commits every Session's Events
for tests to read (agentplane/docs/runner_control.md)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override
from uuid import uuid4

import grpc

from agentplane.grpc_serving import serving
from agentplane.proto import (
    event_log_pb2,
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_ingress_pb2,
    runner_ingress_pb2_grpc,
)
from agentplane.runner.config import ServiceEndpoint
from agentplane.runner.control import RunnerControlClient
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.reconnect import StreamTiming
from agentplane.runner.runner import Runner
from agentplane.service_process import background

# gazelle:include_dep @pypi//protobuf

BEARER = "test-runner-bearer"
# Prompt enough that an in-process runner connects at once and survives a test's idle stretches.
TIMING = StreamTiming(heartbeat_s=0.1, liveness_s=5, backoff_initial_s=0.05, backoff_max_s=0.5)


class ControlListener(runner_control_pb2_grpc.RunnerControlServicer):
    """Sends each request and delivery on the newest runner connection and waits for the runner's answer."""

    def __init__(self) -> None:
        # Connections opened so far; the newest is the one requests go to.
        self.connections = 0
        self._connection: asyncio.Queue[runner_control_pb2.ServiceFrame] | None = None
        self._connected = asyncio.Condition()
        self._results: dict[str, asyncio.Future[runner_control_pb2.ControlResult]] = {}
        self._receipts: dict[tuple[str, str], asyncio.Future[runner_control_pb2.CommandReceipt]] = {}

    async def wait_connected(self, connections: int) -> None:
        """Until the runner holds a connection and has opened at least `connections` in all."""
        async with asyncio.timeout(30), self._connected:
            await self._connected.wait_for(lambda: self.connections >= connections and self._connection is not None)

    async def request(self, request: runner_control_pb2.ControlRequest) -> runner_control_pb2.ControlResult:
        request.request_id = str(uuid4())
        answered: asyncio.Future[runner_control_pb2.ControlResult] = asyncio.get_running_loop().create_future()
        self._results[request.request_id] = answered
        try:
            await self._send(runner_control_pb2.ServiceFrame(control=request))
            return await answered
        finally:
            del self._results[request.request_id]

    async def deliver(self, deliver: runner_control_pb2.DeliverCommand) -> runner_control_pb2.CommandReceipt:
        # Deliveries of one command at once share the first receipt the runner sends for it.
        key = (deliver.session_id, deliver.command.command_id)
        answered = self._receipts.setdefault(key, asyncio.get_running_loop().create_future())
        await self._send(runner_control_pb2.ServiceFrame(deliver=deliver))
        return await asyncio.shield(answered)

    async def _send(self, frame: runner_control_pb2.ServiceFrame) -> None:
        await self.wait_connected(1)
        assert self._connection is not None
        self._connection.put_nowait(frame)

    def _answer(self, frame: runner_control_pb2.RunnerFrame) -> None:
        match frame.WhichOneof("frame"):
            case "control_result":
                if (result := self._results.get(frame.control_result.request_id)) is not None:
                    result.set_result(frame.control_result)
            case "receipt":
                key = (frame.receipt.session_id, frame.receipt.command_id)
                if (receipt := self._receipts.pop(key, None)) is not None:
                    receipt.set_result(frame.receipt)

    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_control_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        await anext(request_iterator)  # Hello
        outgoing: asyncio.Queue[runner_control_pb2.ServiceFrame] = asyncio.Queue()
        async with self._connected:
            self.connections += 1
            self._connection = outgoing
            self._connected.notify_all()
        await context.write(
            runner_control_pb2.ServiceFrame(welcome=runner_control_pb2.Welcome(connection_epoch=self.connections))
        )

        async def send() -> None:
            while True:
                await context.write(await outgoing.get())

        try:
            async with asyncio.TaskGroup() as tasks:
                sending = tasks.create_task(send())
                async for frame in request_iterator:
                    if frame.HasField("heartbeat"):
                        outgoing.put_nowait(runner_control_pb2.ServiceFrame(heartbeat=runner_control_pb2.Heartbeat()))
                    self._answer(frame)
                sending.cancel()
        finally:
            if self._connection is outgoing:
                self._connection = None


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
            RunnerControlClient(runner, target=target, bearer=bearer, timing=TIMING).run(), name="runner control stream"
        ),
        background(IngressPusher(runner, target=target, bearer=bearer, timing=TIMING).run(), name="ingress push"),
    ):
        await gateway.control.wait_connected(connections + 1)
        yield
