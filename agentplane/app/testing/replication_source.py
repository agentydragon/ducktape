"""Scripted harness-host history and Runtime diagnostics peer for app recovery tests.

`ingesting` copies what a test appends into the history database, as the History Service's ingress takes
a runner's pushed journal; its first copy waits for the test to release the `opened` replay.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import NAMESPACE_URL, uuid4, uuid5

import grpc

from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.harness_host import harness_host_pb2, harness_host_pb2_grpc
from agentplane.history_service.testing.history import History
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, session_pb2

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-replication-sandbox"
SESSION = "test-replication-session"
SPEC = session_pb2.SessionSpec(
    placement=session_pb2.SessionPlacement(
        environment_hosted=session_pb2.EnvironmentHostedPlacement(
            environment=session_pb2.ExactEnvironment(name=SANDBOX, uid="test-sandbox-uid"),
            harness=session_pb2.HARNESS_CODEX,
        )
    ),
    model="test-model-before",
    cwd="/test-workspace",
)


@dataclass
class Opened:
    replay: asyncio.Event = field(default_factory=asyncio.Event)


class ReplicationSource:
    """One scripted Runner Session whose operations append admissions to retained history."""

    def __init__(self) -> None:
        self.boot_id = str(uuid4())
        self.session_id = uuid5(NAMESPACE_URL, SANDBOX + "/" + SESSION)
        self.entries: list[event_log_pb2.EventEntry] = []
        self.attached = session_pb2.Attached(
            session_id=SESSION,
            spec=SPEC,
            harness_state=session_pb2.HARNESS_STATE_RUNNING,
            setup_state=session_pb2.SETUP_STATE_NOT_REQUIRED,
        )
        self.opened: asyncio.Queue[Opened] = asyncio.Queue()
        self.runner = Runner(self)
        self._appended = asyncio.Event()
        self.activity_updates: set[asyncio.Event] = set()

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
        for update in self.activity_updates:
            update.set()
        return entry

    def confirm_input(
        self, command: command_pb2.Command, message_id: str, *, turn_id: str = "test-browser-turn"
    ) -> None:
        """Confirm the text of a submitted input; `turn_id` defaults to the turn `thread_source` starts."""
        self.append(
            event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    harness_message_id=message_id,
                    origin_command_ids=[command.command_id],
                    text=command.submit_input.text,
                    turn_id=turn_id,
                )
            )
        )

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
                    entries, attached = self.entries[copied:], session_pb2.Attached()
                    # The runner's snapshot at the last copied cursor, taken with the entries it covers.
                    attached.CopyFrom(self.attached)
                    await history.record(self.session_id, entries, attached=attached)
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
    """Serve HarnessHost status beside Runtime process and lifecycle diagnostics."""
    harness_host_pb2_grpc.add_HarnessHostServicer_to_server(source.runner, server)
    environment_runtime_pb2_grpc.add_EnvironmentRuntimeServicer_to_server(RuntimeDiagnostics(source), server)


class RuntimeDiagnostics(environment_runtime_pb2_grpc.EnvironmentRuntimeServicer):
    """Serve only boot diagnostics; the test's readiness path reads HarnessHost inventory."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source

    @override
    async def GetStatus(
        self, request: session_pb2.GetStatusRequest, context: grpc.aio.ServicerContext
    ) -> session_pb2.EnvironmentRuntimeStatus:
        return session_pb2.EnvironmentRuntimeStatus(environment_runtime_boot_id=self.source.boot_id)


class Runner(harness_host_pb2_grpc.HarnessHostServicer):
    """A scripted Runner whose per-operation RPCs append command admissions to retained history."""

    def __init__(self, source: ReplicationSource) -> None:
        self.source = source
        self.commands: asyncio.Queue[command_pb2.Command] = asyncio.Queue()
        self._admissions: dict[str, event_log_pb2.EventEntry] = {}

    @override
    async def CreateSession(
        self, request: harness_host_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.CreateSessionResponse:
        if (
            request.session_id != str(self.source.session_id)
            or request.spec != self.source.attached.spec
            or request.HasField("setup_script")
        ):
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "conflicting scripted Session")
        attached = session_pb2.Attached()
        attached.CopyFrom(self.source.attached)
        attached.session_id = request.session_id
        return harness_host_pb2.CreateSessionResponse(attached=attached)

    def session(self) -> harness_host_pb2.Session:
        return harness_host_pb2.Session(
            session_id=str(self.source.session_id),
            active_turn_id=self.source.attached.active_turn_id,
            environment_hosted=harness_host_pb2.EnvironmentHostedSession(
                setup=self.source.attached.setup_state,
                harness=harness_host_pb2.HarnessAttachment(state=harness_host_pb2.HarnessAttachment.STATE_ATTACHED),
            ),
        )

    @override
    async def ListSessions(
        self, request: harness_host_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.ListSessionsResponse:
        await context.send_initial_metadata((("x-agentplane-harness-host-session-version", "1"),))
        return harness_host_pb2.ListSessionsResponse(sessions=[self.session()])

    @override
    async def WatchSessions(
        self, request: harness_host_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[harness_host_pb2.WatchSessionsResponse]:
        await context.send_initial_metadata((("x-agentplane-harness-host-session-version", "1"),))
        update = asyncio.Event()
        self.source.activity_updates.add(update)
        try:
            while True:
                update.clear()
                yield harness_host_pb2.WatchSessionsResponse(upsert=self.session())
                yield harness_host_pb2.WatchSessionsResponse(caught_up=harness_host_pb2.CaughtUp())
                await update.wait()
        finally:
            self.source.activity_updates.remove(update)

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
        self, request: harness_host_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.SubmitInputResponse:
        command = command_pb2.Command(
            command_id=request.command_id, submit_input=command_pb2.SubmitInput(text=request.text)
        )
        return harness_host_pb2.SubmitInputResponse(admission=await self.admit(command, context))

    @override
    async def ChangeModel(
        self, request: harness_host_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.ChangeModelResponse:
        command = command_pb2.Command(
            command_id=request.command_id, change_model=command_pb2.ChangeModel(model=request.model)
        )
        return harness_host_pb2.ChangeModelResponse(admission=await self.admit(command, context))

    @override
    async def ChangeReasoningEffort(
        self, request: harness_host_pb2.ChangeReasoningEffortRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.ChangeReasoningEffortResponse:
        command = command_pb2.Command(
            command_id=request.command_id,
            change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=request.effort),
        )
        return harness_host_pb2.ChangeReasoningEffortResponse(admission=await self.admit(command, context))

    @override
    async def InterruptTurn(
        self, request: harness_host_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> harness_host_pb2.InterruptTurnResponse:
        command = command_pb2.Command(
            command_id=request.command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=request.turn_id)
        )
        return harness_host_pb2.InterruptTurnResponse(admission=await self.admit(command, context))
