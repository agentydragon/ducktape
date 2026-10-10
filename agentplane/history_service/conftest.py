"""A scripted Sandbox Service serving WatchSessions and FollowSession over in-memory journals.

It owns no Store: it plays the runner journals and the Session feed the ingester under test
subscribes to, including frames no runner sends yet (`sealed`).
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import pytest
from google.protobuf.empty_pb2 import Empty

from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//protobuf


@dataclass
class Journal:
    change: protocol_pb2.SessionChange
    entries: list[event_log_pb2.EventEntry] = field(default_factory=list)
    sealed: bool = False


@dataclass(frozen=True)
class Follow:
    bearer: str
    request: protocol_pb2.FollowSessionRequest


class ScriptedSandboxService:
    def __init__(self, *, lease_s: float) -> None:
        self.lease_s = lease_s
        self.journals: dict[str, Journal] = {}
        self.follows: asyncio.Queue[Follow] = asyncio.Queue()
        self.following: Counter[str] = Counter()
        # While set to an unset Event, follows pause after their Attached snapshot.
        self.replay_gate: asyncio.Event | None = None
        self._position = 0
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

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

    async def watch(
        self, request: protocol_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.WatchSessionsResponse]:
        deadline = asyncio.get_running_loop().time() + self.lease_s
        position = request.after_position
        while True:
            changed = self._changed
            for journal in sorted(self.journals.values(), key=lambda journal: journal.change.position):
                if journal.change.position > position:
                    yield protocol_pb2.WatchSessionsResponse(change=journal.change)
                    position = journal.change.position
            try:
                async with asyncio.timeout_at(deadline):
                    await changed.wait()
            except TimeoutError:
                break
        yield protocol_pb2.WatchSessionsResponse(reconnect_required=Empty())

    async def follow(
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        [bearer] = [value for key, value in context.invocation_metadata() or () if key == "authorization"]
        assert isinstance(bearer, str)
        self.follows.put_nowait(Follow(bearer, request))
        destination = request.destination
        journal = self.journals.get(destination.session_id)
        if journal is None or (
            destination.sandbox.owner,
            destination.sandbox.sandbox,
            destination.sandbox.sandbox_uid,
        ) != (journal.change.owner, journal.change.sandbox, journal.change.sandbox_uid):
            await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
        assert journal is not None
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
            deadline = asyncio.get_running_loop().time() + self.lease_s
            while True:
                changed = self._changed
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

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        server = grpc.aio.server()
        # Only the subscriber's calls; anything else fails UNIMPLEMENTED.
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    "ducktape.agentplane.sandbox.v1.SandboxService",
                    {
                        "WatchSessions": grpc.unary_stream_rpc_method_handler(
                            self.watch,
                            request_deserializer=protocol_pb2.WatchSessionsRequest.FromString,
                            response_serializer=protocol_pb2.WatchSessionsResponse.SerializeToString,
                        ),
                        "FollowSession": grpc.unary_stream_rpc_method_handler(
                            self.follow,
                            request_deserializer=protocol_pb2.FollowSessionRequest.FromString,
                            response_serializer=protocol_pb2.FollowSessionResponse.SerializeToString,
                        ),
                    },
                )
            ]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            yield f"127.0.0.1:{port}"
        finally:
            await server.stop(0)


@asynccontextmanager
async def subscriber(target: str, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    client = SandboxServiceClient(
        target,
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
def sandbox_service() -> ScriptedSandboxService:
    return ScriptedSandboxService(lease_s=0.5)
