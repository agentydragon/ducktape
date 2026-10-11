"""Scripted Sandboxes serving WatchSessions, FollowSession and retention holds over
in-memory journals.

It owns no Store: it plays the runner journals and the Session feed the ingester under test
subscribes to, including the `sealed` frame that ends a Session's incarnation, and keeps the holds
its teardown would wait on.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import grpc
import pytest
from google.protobuf.empty_pb2 import Empty

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner_gateway import protocol_pb2_grpc as gateway_pb2_grpc
from agentplane.sandbox_service import protocol_pb2, protocol_pb2_grpc
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//protobuf


@dataclass
class Journal:
    change: protocol_pb2.SessionChange
    entries: list[event_log_pb2.EventEntry] = field(default_factory=list)
    sealed: bool = False
    holds: dict[str, protocol_pb2.Hold] = field(default_factory=dict)

    def deletable(self) -> bool:
        """Whether teardown would delete the Sandbox: sealed, and every hold confirmed through the seal."""
        return self.sealed and all(hold.confirmed_through >= len(self.entries) for hold in self.holds.values())


@dataclass(frozen=True)
class Follow:
    bearer: str
    request: protocol_pb2.FollowSessionRequest
    # The holders whose hold was in place when the follow started.
    holders: frozenset[str]


class ScriptedSandboxes:
    """Sandboxes' journals and holds, served the way the ingester sees them: the Runner Gateway
    feeds and follows Sessions, the Sandbox Service keeps retention holds."""

    def __init__(self, *, lease_s: float) -> None:
        self.lease_s = lease_s
        self.journals: dict[str, Journal] = {}
        self.changed = asyncio.Event()
        self._position = 0
        self.gateway = ScriptedGateway(self)
        self.holds = ScriptedHolds(self)

    def _notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def add(self, session_id: str, *, sandbox: str, sandbox_uid: str, owner: protocol_pb2.ServiceAccount) -> None:
        self._position += 1
        change = protocol_pb2.SessionChange(
            session_id=session_id, sandbox=sandbox, sandbox_uid=sandbox_uid, position=self._position, owner=owner
        )
        self.journals[session_id] = Journal(change)
        self._notify()

    def append(self, session_id: str, *entries: event_log_pb2.EventEntry) -> None:
        self.journals[session_id].entries.extend(entries)
        self._notify()

    def seal(self, session_id: str) -> None:
        self.journals[session_id].sealed = True
        self._notify()

    async def journal(self, destination: protocol_pb2.SessionDestination, context: grpc.aio.ServicerContext) -> Journal:
        journal = self.journals.get(destination.session_id)
        if journal is None or (
            destination.sandbox.owner,
            destination.sandbox.sandbox,
            destination.sandbox.sandbox_uid,
        ) != (journal.change.owner, journal.change.sandbox, journal.change.sandbox_uid):
            await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
        assert journal is not None
        return journal

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        server = grpc.aio.server()
        gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(self.gateway, server)
        protocol_pb2_grpc.add_SandboxServiceServicer_to_server(self.holds, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            yield f"127.0.0.1:{port}"
        finally:
            await server.stop(0)


class ScriptedGateway(gateway_pb2_grpc.RunnerGatewayServicer):
    """Scripts only the subscriber's feed and follows; anything else answers UNIMPLEMENTED."""

    def __init__(self, sandboxes: ScriptedSandboxes) -> None:
        self.sandboxes = sandboxes
        self.follows: asyncio.Queue[Follow] = asyncio.Queue()
        self.following: Counter[str] = Counter()
        # While set to an unset Event, follows pause after their Attached snapshot.
        self.replay_gate: asyncio.Event | None = None
        # The next this many WatchSessions calls fail UNAVAILABLE.
        self.watch_failures = 0
        # The client address of every WatchSessions call; a new address means a new connection.
        self.watch_peers: asyncio.Queue[str] = asyncio.Queue()

    @override
    async def WatchSessions(
        self, request: protocol_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.WatchSessionsResponse]:
        self.watch_peers.put_nowait(context.peer())
        if self.watch_failures:
            self.watch_failures -= 1
            await context.abort(grpc.StatusCode.UNAVAILABLE, "scripted feed outage")
        deadline = asyncio.get_running_loop().time() + self.sandboxes.lease_s
        position = request.after_position
        while True:
            changed = self.sandboxes.changed
            for journal in sorted(self.sandboxes.journals.values(), key=lambda journal: journal.change.position):
                if journal.change.position > position:
                    yield protocol_pb2.WatchSessionsResponse(change=journal.change)
                    position = journal.change.position
            try:
                async with asyncio.timeout_at(deadline):
                    await changed.wait()
            except TimeoutError:
                break
        yield protocol_pb2.WatchSessionsResponse(reconnect_required=Empty())

    @override
    async def FollowSession(
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        [bearer] = [value for key, value in context.invocation_metadata() or () if key == "authorization"]
        assert isinstance(bearer, str)
        destination = request.destination
        journal = await self.sandboxes.journal(destination, context)
        self.follows.put_nowait(Follow(bearer, request, frozenset(journal.holds)))
        cursor = request.follow.after_cursor
        if cursor > len(journal.entries):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "cursor beyond the journal")
        self.following[destination.session_id] += 1
        try:
            yield protocol_pb2.FollowSessionResponse(
                attached=runner_pb2.Attached(session_id=destination.session_id, last_cursor=len(journal.entries))
            )
            if self.replay_gate is not None:
                await self.replay_gate.wait()
            deadline = asyncio.get_running_loop().time() + self.sandboxes.lease_s
            while True:
                changed = self.sandboxes.changed
                while cursor < len(journal.entries):
                    yield protocol_pb2.FollowSessionResponse(entry=journal.entries[cursor])
                    cursor += 1
                if journal.sealed:
                    yield protocol_pb2.FollowSessionResponse(sealed=protocol_pb2.Sealed(cursor=cursor))
                    return
                try:
                    async with asyncio.timeout_at(deadline):
                        await changed.wait()
                except TimeoutError:
                    break
            yield protocol_pb2.FollowSessionResponse(reconnect_required=Empty())
        finally:
            self.following[destination.session_id] -= 1


class ScriptedHolds(protocol_pb2_grpc.SandboxServiceServicer):
    """Scripts only retention holds; anything else answers UNIMPLEMENTED."""

    def __init__(self, sandboxes: ScriptedSandboxes) -> None:
        self.sandboxes = sandboxes
        # Every hold confirmation, in arrival order.
        self.confirmations: asyncio.Queue[protocol_pb2.Hold] = asyncio.Queue()

    @override
    async def PlaceHold(
        self, request: protocol_pb2.HoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        journal = await self.sandboxes.journal(request.destination, context)
        return journal.holds.setdefault(
            request.holder, protocol_pb2.Hold(session_id=request.destination.session_id, holder=request.holder)
        )

    @override
    async def ConfirmHold(
        self, request: protocol_pb2.ConfirmHoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        journal = await self.sandboxes.journal(request.destination, context)
        hold = journal.holds.get(request.holder)
        if hold is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "no such hold")
        assert hold is not None
        hold.confirmed_through = max(hold.confirmed_through, request.through_cursor)
        snapshot = protocol_pb2.Hold()
        snapshot.CopyFrom(hold)  # the stored hold keeps changing
        self.confirmations.put_nowait(snapshot)
        return hold


@asynccontextmanager
async def subscriber(target: str, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    client = SandboxServiceClient(
        target,
        gateway_target=target,
        namespace=None,
        token_file=token_file,
        command_admission_timeout_s=None,
        request_timeout_s=5,
        lifecycle_timeout_s=5,
        follow_timeout_s=30,
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def sandboxes() -> ScriptedSandboxes:
    return ScriptedSandboxes(lease_s=0.5)
