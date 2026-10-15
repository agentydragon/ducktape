"""A test-only Dispatcher client adapter for runner-backed app scenarios."""

from typing import override

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.proto import command_pb2, runner_pb2, session_dispatcher_pb2


class GatewayBackedDispatcher(SessionDispatcherClient):
    """Keep integration tests focused on app routing while a real runner handles each command.

    The production app never submits commands to the Runner Gateway. This adapter gives the
    app's required Dispatcher interface to tests whose purpose is exercising runner effects;
    Dispatcher queue and retry semantics have their own service tests.
    """

    def __init__(self, runners: RunnerDirectory) -> None:
        self.runners = runners
        self.dispatches: dict[str, session_dispatcher_pb2.Dispatch] = {}

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
        raise AssertionError("these runner-backed tests keep the OpenSession gate disabled")

    @override
    async def get_session_opening(self, session_id: str) -> session_dispatcher_pb2.SessionOpening:
        raise AssertionError("these runner-backed tests keep the OpenSession gate disabled")
