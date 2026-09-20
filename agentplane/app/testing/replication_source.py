"""Controlled runner Event source; no claim about proprietary harness execution."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import grpc

from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, model="test-model-before", cwd="/test-workspace")


@dataclass
class Opened:
    after_cursor: int
    replay: asyncio.Event = field(default_factory=asyncio.Event)


class ReplicationSource:
    def __init__(self) -> None:
        self.entries: list[event_log_pb2.EventEntry] = []
        self.attached = protocol_pb2.Attached(
            session_id=SESSION, spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, command_pb2.Command] = {}
        self._changed = asyncio.Event()

    def append(self, event: event_pb2.Event) -> event_log_pb2.EventEntry:
        cursor = len(self.entries) + 1
        event.at.FromSeconds(1_700_000_000 + cursor)
        entry = event_log_pb2.EventEntry(
            cursor=cursor,
            origin=event_log_pb2.EventOrigin(source_id="test-retained-runner-source", sequence=cursor),
            event=event,
        )
        self.entries.append(entry)
        self.attached.last_cursor = cursor
        self._changed.set()
        return entry

    async def attach(
        self, requests: AsyncIterator[protocol_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.ServerMessage]:
        del context
        first = await anext(requests)
        assert first.HasField("open")
        assert first.open.session_id == SESSION
        opened = Opened(first.open.follow.after_cursor)
        self.opened.put_nowait(opened)
        yield protocol_pb2.ServerMessage(attached=self.attached)
        detached = asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._commands(requests, opened, detached))
            # Tests can hold an ingestion stream behind its truthful current snapshot. A command
            # attachment releases its own replay so it can observe that command's admission.
            await opened.replay.wait()
            cursor = opened.after_cursor
            while True:
                self._changed.clear()
                while cursor < len(self.entries):
                    entry = self.entries[cursor]
                    yield protocol_pb2.ServerMessage(event_entry=entry)
                    cursor = entry.cursor
                if detached.is_set():
                    return
                await self._changed.wait()

    async def _commands(
        self, requests: AsyncIterator[protocol_pb2.ClientMessage], opened: Opened, detached: asyncio.Event
    ) -> None:
        try:
            async for message in requests:
                if message.HasField("detach"):
                    return
                assert message.HasField("command")
                command = message.command
                self.commands.put_nowait(command)
                if previous := self._admissions.get(command.command_id):
                    assert command == previous, "test source received a conflicting command identity"
                else:
                    self._admissions[command.command_id] = command
                    self.append(event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))
                opened.replay.set()
        finally:
            detached.set()
            self._changed.set()

    async def list_sessions(
        self, request: protocol_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListSessionsResponse:
        del request, context
        return protocol_pb2.ListSessionsResponse(
            sessions=[
                protocol_pb2.SessionSummary(
                    session_id=SESSION,
                    spec=self.attached.spec,
                    last_cursor=self.attached.last_cursor,
                    harness_state=self.attached.harness_state,
                    active_turn_id=self.attached.active_turn_id,
                )
            ]
        )

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    "ducktape.agentplane.runner.v1.Runner",
                    {
                        "Attach": grpc.stream_stream_rpc_method_handler(
                            self.attach,
                            request_deserializer=protocol_pb2.ClientMessage.FromString,
                            response_serializer=protocol_pb2.ServerMessage.SerializeToString,
                        ),
                        "ListSessions": grpc.unary_unary_rpc_method_handler(
                            self.list_sessions,
                            request_deserializer=protocol_pb2.ListSessionsRequest.FromString,
                            response_serializer=protocol_pb2.ListSessionsResponse.SerializeToString,
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
