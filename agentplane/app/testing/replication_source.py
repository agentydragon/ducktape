"""Scripted Sandbox Service peer for app process and browser recovery tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

import grpc

from agentplane.app.testing.history_service import HistoryService
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.sandbox_service import protocol_pb2 as service_pb2

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, model="test-model-before", cwd="/test-workspace")


@dataclass
class Opened:
    after_cursor: int
    replay: asyncio.Event = field(default_factory=asyncio.Event)


class ReplicationSource(HistoryService):
    def __init__(self, peer: HistoryService | None = None) -> None:
        super().__init__()
        if peer is not None:
            self.histories = peer.histories
        self.session_id = uuid5(NAMESPACE_URL, SANDBOX + "/" + SESSION)
        self.open(self.session_id)
        self.entries = self.histories[str(self.session_id)].entries
        self.attached = protocol_pb2.Attached(
            session_id=SESSION, spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.hold_next_replay = True
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, event_log_pb2.EventEntry] = {}

    def append(self, event: event_pb2.Event) -> event_log_pb2.EventEntry:
        cursor = len(self.entries) + 1
        event.at.FromSeconds(1_700_000_000 + cursor)
        entry = event_log_pb2.EventEntry(
            cursor=cursor,
            origin=event_log_pb2.EventOrigin(source_id="test-retained-runner-source", sequence=cursor),
            event=event,
        )
        self.publish(self.session_id, [entry])
        self.attached.last_cursor = cursor
        self.set_feed(self.session_id, service_pb2.SessionFeedState(attached=self.attached))
        return self.entries[-1]

    async def read_events(
        self, request: service_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> service_pb2.ReadSessionEventsResponse:
        if self.hold_next_replay:
            self.hold_next_replay = False
            opened = Opened(request.after_cursor)
            self.opened.put_nowait(opened)
            await opened.replay.wait()
        self.set_feed(self.session_id, service_pb2.SessionFeedState(attached=self.attached))
        return await super().read_events(request, context)

    async def submit_command(
        self, request: service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        command = request.command
        self.commands.put_nowait(command)
        if previous := self._admissions.get(command.command_id):
            if previous.event.command_admitted.command != command:
                await context.abort(grpc.StatusCode.ALREADY_EXISTS, "conflicting command")
            return previous
        entry = self.append(event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))
        self._admissions[command.command_id] = entry
        return entry

    async def list_sessions(
        self, request: service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListSessionsResponse:
        return protocol_pb2.ListSessionsResponse(
            sessions=[
                protocol_pb2.SessionSummary(
                    session_id=str(self.session_id),
                    spec=self.attached.spec,
                    last_cursor=self.attached.last_cursor,
                    harness_state=self.attached.harness_state,
                    active_turn_id=self.attached.active_turn_id,
                )
            ]
        )

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    "ducktape.agentplane.sandbox.v1.SandboxService",
                    {
                        "ReadSessionEvents": grpc.unary_unary_rpc_method_handler(
                            self.read_events,
                            request_deserializer=service_pb2.ReadSessionEventsRequest.FromString,
                            response_serializer=service_pb2.ReadSessionEventsResponse.SerializeToString,
                        ),
                        "ReadSessionObservations": grpc.unary_unary_rpc_method_handler(
                            self.read_observations,
                            request_deserializer=service_pb2.ReadSessionObservationsRequest.FromString,
                            response_serializer=service_pb2.ReadSessionObservationsResponse.SerializeToString,
                        ),
                        "SubmitCommand": grpc.unary_unary_rpc_method_handler(
                            self.submit_command,
                            request_deserializer=service_pb2.SubmitCommandRequest.FromString,
                            response_serializer=event_log_pb2.EventEntry.SerializeToString,
                        ),
                        "ListSessions": grpc.unary_unary_rpc_method_handler(
                            self.list_sessions,
                            request_deserializer=service_pb2.SandboxRequest.FromString,
                            response_serializer=protocol_pb2.ListSessionsResponse.SerializeToString,
                        ),
                    },
                )
            ]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            yield port
        finally:
            await server.stop(0)
