"""A test-only Dispatcher client adapter for runner-backed app scenarios."""

from typing import override
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.proto import command_pb2, runner_pb2, session_dispatcher_pb2


class GatewayBackedDispatcher(SessionDispatcherClient):
    """Keep runner-backed app tests focused on app effects, using a real local Runner Gateway.

    This test adapter sends fixture operations to the local Runner Gateway and creates the test
    Thread row immediately, as the old direct-open path did. It is not evidence for Dispatcher
    queueing or app production routing; Dispatcher queue, retry, and deadline behavior have
    separate service/client tests.
    """

    def __init__(self, runners: RunnerDirectory, event_logs: EventLogStore) -> None:
        self.runners = runners
        self.event_logs = event_logs
        self.dispatches: dict[str, session_dispatcher_pb2.Dispatch] = {}
        self.openings: dict[str, session_dispatcher_pb2.SessionOpening] = {}

    @override
    async def command(
        self, *, sandbox: str, session_id: str, command: command_pb2.Command
    ) -> session_dispatcher_pb2.Dispatch:
        runner = self.runners.client(sandbox)
        admission = await self.runners.gateway.command(runner.destination, session_id, command)
        operation = {
            "submit_input": session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
            "change_model": session_dispatcher_pb2.OPERATION_CHANGE_MODEL,
            "change_reasoning_effort": session_dispatcher_pb2.OPERATION_CHANGE_REASONING_EFFORT,
            "stop_runner_session": session_dispatcher_pb2.OPERATION_STOP_RUNNER_SESSION,
        }.get(command.WhichOneof("operation"), session_dispatcher_pb2.OPERATION_UNSPECIFIED)
        dispatch = session_dispatcher_pb2.Dispatch(
            command_id=command.command_id,
            session_id=session_id,
            sandbox=sandbox,
            sandbox_uid=runner.destination.sandbox_uid,
            operation=operation,
            state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
            admission=admission,
        )
        self.dispatches[command.command_id] = dispatch
        return dispatch

    @override
    async def get_dispatch(self, command_id: str) -> session_dispatcher_pb2.Dispatch:
        if dispatch := self.dispatches.get(command_id):
            return dispatch
        return session_dispatcher_pb2.Dispatch(
            command_id=command_id, state=session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN
        )

    @override
    async def open_session(
        self, *, sandbox: str, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None
    ) -> session_dispatcher_pb2.SessionOpening:
        runner = self.runners.client(sandbox)
        created = await runner.create(
            session_id=UUID(session_id),
            spec=MessageToDict(spec, always_print_fields_with_no_presence=True),
            setup_script=setup_script,
        )
        await self.event_logs.open(created.session_id)
        opening = session_dispatcher_pb2.SessionOpening(
            session_id=created.session_id, sandbox=sandbox, state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
        )
        if created.HasField("attached"):
            opening.attached.CopyFrom(created.attached)
        self.openings[created.session_id] = opening
        return opening

    @override
    async def get_session_opening(self, session_id: str) -> session_dispatcher_pb2.SessionOpening:
        return self.openings.get(
            session_id,
            session_dispatcher_pb2.SessionOpening(
                session_id=session_id, state=session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN
            ),
        )
