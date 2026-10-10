"""Scripted runner history and Runner Gateway peer for app process and browser recovery tests.

`ingesting` copies what a test appends into the history database, as the History Service ingester
copies a runner's journal; its first copy waits for the test to release the `opened` replay.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import NAMESPACE_URL, uuid5

import grpc

from agentplane.history_service.testing.history import History
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    event_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
)

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model-before", cwd="/test-workspace")


@dataclass
class Opened:
    replay: asyncio.Event = field(default_factory=asyncio.Event)


class ReplicationSource:
    """One runner's Session; `gateway` admits its commands."""

    def __init__(self) -> None:
        self.session_id = uuid5(NAMESPACE_URL, SANDBOX + "/" + SESSION)
        self.entries: list[event_log_pb2.EventEntry] = []
        self.attached = runner_pb2.Attached(
            session_id=SESSION, spec=SPEC, harness_state=runner_pb2.HARNESS_STATE_RUNNING
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.gateway = Gateway(self)
        self._appended = asyncio.Event()

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
        self._appended.set()
        return entry

    @asynccontextmanager
    async def ingesting(self, history: History) -> AsyncIterator[None]:
        async def copy() -> None:
            opened = Opened()
            self.opened.put_nowait(opened)
            await opened.replay.wait()
            copied = 0
            while True:
                self._appended.clear()
                if copied < len(self.entries):
                    entries, attached = self.entries[copied:], runner_pb2.Attached()
                    # The runner's snapshot at the last copied cursor, taken with the entries it covers.
                    attached.CopyFrom(self.attached)
                    await history.record(self.session_id, entries)
                    await history.set_attached(self.session_id, attached)
                    copied += len(entries)
                    continue
                await self._appended.wait()

        task = asyncio.create_task(copy(), name="test-replication-copy")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def add_replication_source(source: ReplicationSource, server: grpc.aio.Server) -> None:
    """Registers the source's gateway, the service the app dials for commands and Session listings."""
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(source.gateway, server)


class Gateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """The Runner Gateway in front of the source's runner: an admitted command lands in its history."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, event_log_pb2.EventEntry] = {}

    @override
    async def SubmitCommand(
        self, request: sandbox_service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
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
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
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
