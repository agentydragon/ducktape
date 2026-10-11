"""Scripted Sandbox Service and History Service peer for app process and browser recovery tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import NAMESPACE_URL, uuid5

import grpc

from agentplane.app.testing.history_service import HistoryService
from agentplane.history_service import protocol_pb2 as history_pb2, protocol_pb2_grpc as history_pb2_grpc
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway import protocol_pb2_grpc as gateway_pb2_grpc
from agentplane.sandbox_service import protocol_pb2 as service_pb2

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model-before", cwd="/test-workspace")


@dataclass
class Opened:
    after_cursor: int
    replay: asyncio.Event = field(default_factory=asyncio.Event)


class ReplicationSource(HistoryService):
    """The History Service the app replicates one Session from; `gateway` admits its commands."""

    def __init__(self, peer: HistoryService | None = None) -> None:
        super().__init__()
        if peer is not None:
            self.histories = peer.histories
        self.session_id = uuid5(NAMESPACE_URL, SANDBOX + "/" + SESSION)
        self.open(self.session_id)
        self.entries = self.histories[str(self.session_id)].entries
        self.attached = runner_pb2.Attached(
            session_id=SESSION, spec=SPEC, harness_state=runner_pb2.HARNESS_STATE_RUNNING
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.hold_next_replay = True
        self.gateway = Gateway(self)

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
        self.set_feed(self.session_id, history_pb2.SessionFeedState(attached=self.attached))
        return self.entries[-1]

    @override
    async def ReadSessionEvents(
        self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionEventsResponse:
        if self.hold_next_replay:
            self.hold_next_replay = False
            opened = Opened(request.after_cursor)
            self.opened.put_nowait(opened)
            await opened.replay.wait()
        self.set_feed(self.session_id, history_pb2.SessionFeedState(attached=self.attached))
        return await super().ReadSessionEvents(request, context)

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        server = grpc.aio.server()
        history_pb2_grpc.add_HistoryServiceServicer_to_server(self, server)
        gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(self.gateway, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            yield port
        finally:
            await server.stop(0)


class Gateway(gateway_pb2_grpc.RunnerGatewayServicer):
    """The Runner Gateway in front of the source's runner: an admitted command lands in its history."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, event_log_pb2.EventEntry] = {}

    @override
    async def SubmitCommand(
        self, request: service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        command = request.command
        self.commands.put_nowait(command)
        if previous := self._admissions.get(command.command_id):
            if previous.event.command_admitted.command != command:
                await context.abort(grpc.StatusCode.ALREADY_EXISTS, "conflicting command")
            return previous
        entry = self.source.append(event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))
        self._admissions[command.command_id] = entry
        return entry

    @override
    async def ListSessions(
        self, request: service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return runner_pb2.ListSessionsResponse(
            sessions=[
                runner_pb2.SessionSummary(
                    session_id=str(self.source.session_id),
                    spec=self.source.attached.spec,
                    last_cursor=self.source.attached.last_cursor,
                    harness_state=self.source.attached.harness_state,
                    active_turn_id=self.source.attached.active_turn_id,
                )
            ]
        )
