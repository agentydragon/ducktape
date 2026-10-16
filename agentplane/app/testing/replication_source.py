"""Scripted runner history and Runner Gateway peer for app process and browser recovery tests.

`ingesting` copies what a test appends into the history database, as the History Service's ingress takes
a runner's pushed journal; its first copy waits for the test to release the `opened` replay.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import NAMESPACE_URL, UUID, uuid5

import grpc

from agentplane.history_service.testing.history import History
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    event_pb2,
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
)

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model-before", cwd="/test-workspace")


@dataclass
class Opened:
    replay: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Placements:
    """Where each Session a test opened runs: what the Runner Gateway's registry would answer."""

    sandboxes: dict[UUID, str] = field(default_factory=dict)

    def get(self, session_ids: Iterable[str]) -> list[runner_gateway_pb2.SessionSandbox]:
        wanted = set(session_ids)
        return self._matching(lambda session_id, _: str(session_id) in wanted)

    def _matching(self, keep: Callable[[UUID, str], bool]) -> list[runner_gateway_pb2.SessionSandbox]:
        return [
            runner_gateway_pb2.SessionSandbox(session_id=str(session_id), sandbox=sandbox)
            for session_id, sandbox in self.sandboxes.items()
            if keep(session_id, sandbox)
        ]


class ReplicationSource:
    """One runner's Session; `gateway` admits its commands and answers where Sessions run from `placements`."""

    def __init__(self, placements: Placements) -> None:
        self.placements = placements
        self.session_id = uuid5(NAMESPACE_URL, SANDBOX + "/" + SESSION)
        placements.sandboxes[self.session_id] = SANDBOX
        self.entries: list[event_log_pb2.EventEntry] = []
        self.attached = runner_pb2.Attached(
            session_id=SESSION, spec=SPEC, harness_state=runner_pb2.HARNESS_STATE_RUNNING
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.gateway = Gateway(self)
        self.runner = Runner(self)
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
    """Registers placement reads at Gateway and command/list RPCs at the Runner proxy."""
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(source.gateway, server)
    runner_control_pb2_grpc.add_RunnerServicer_to_server(source.runner, server)


class Gateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """The Runner Gateway's retained Session-placement reads."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source

    @override
    async def BatchGetSessionSandboxes(
        self, request: runner_gateway_pb2.BatchGetSessionSandboxesRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.BatchGetSessionSandboxesResponse:
        return runner_gateway_pb2.BatchGetSessionSandboxesResponse(
            sessions=self.source.placements.get(request.session_ids)
        )


class Runner(runner_control_pb2_grpc.RunnerServicer):
    """A scripted Runner whose per-operation RPCs append command admissions to retained history."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, event_log_pb2.EventEntry] = {}

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
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

    async def admit(self, command: command_pb2.Command, context: grpc.aio.ServicerContext) -> event_log_pb2.EventEntry:
        if previous := self._admissions.get(command.command_id):
            if previous.event.command_admitted.command != command:
                await context.abort(grpc.StatusCode.ALREADY_EXISTS, "conflicting command")
            return previous
        self.commands.put_nowait(command)
        entry = self.source.append(event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)))
        self._admissions[command.command_id] = entry
        return entry

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        command = command_pb2.Command(
            command_id=request.command_id, submit_input=command_pb2.SubmitInput(text=request.text)
        )
        return runner_control_pb2.SubmitInputResponse(admission=await self.admit(command, context))

    @override
    async def ChangeModel(
        self, request: runner_control_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeModelResponse:
        command = command_pb2.Command(
            command_id=request.command_id, change_model=command_pb2.ChangeModel(model=request.model)
        )
        return runner_control_pb2.ChangeModelResponse(admission=await self.admit(command, context))

    @override
    async def ChangeReasoningEffort(
        self, request: runner_control_pb2.ChangeReasoningEffortRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeReasoningEffortResponse:
        command = command_pb2.Command(
            command_id=request.command_id,
            change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=request.effort),
        )
        return runner_control_pb2.ChangeReasoningEffortResponse(admission=await self.admit(command, context))

    @override
    async def InterruptTurn(
        self, request: runner_control_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.InterruptTurnResponse:
        command = command_pb2.Command(
            command_id=request.command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=request.turn_id)
        )
        return runner_control_pb2.InterruptTurnResponse(admission=await self.admit(command, context))

    @override
    async def StopRunnerSession(
        self, request: runner_control_pb2.StopRunnerSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.StopRunnerSessionResponse:
        command = command_pb2.Command(
            command_id=request.command_id, stop_runner_session=command_pb2.StopRunnerSession()
        )
        return runner_control_pb2.StopRunnerSessionResponse(admission=await self.admit(command, context))
