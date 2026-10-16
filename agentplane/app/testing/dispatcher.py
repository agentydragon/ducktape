"""A test-only Dispatcher client adapter for runner-backed app scenarios."""

from typing import override
from uuid import UUID

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.proto import command_pb2, runner_control_pb2, runner_pb2, session_dispatcher_pb2

# The generated protobuf stubs import google.protobuf for mypy.
# gazelle:include_dep @pypi//protobuf


class RunnerProxyBackedDispatcher(SessionDispatcherClient):
    """Keep runner-backed app tests focused on app effects, using a real local Runner proxy.

    This test adapter sends fixture operations to per-operation Runner RPCs and creates the test
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
        common = {"session_id": session_id, "command_id": command.command_id}
        operation = session_dispatcher_pb2.OPERATION_UNSPECIFIED
        match command.WhichOneof("operation"):
            case "submit_input":
                response = await runner.control.runner_unary(
                    runner.destination,
                    lambda stub: stub.SubmitInput,
                    runner_control_pb2.SubmitInputRequest(**common, text=command.submit_input.text),
                )
                operation = session_dispatcher_pb2.OPERATION_SUBMIT_INPUT
            case "change_model":
                response = await runner.control.runner_unary(
                    runner.destination,
                    lambda stub: stub.ChangeModel,
                    runner_control_pb2.ChangeModelRequest(**common, model=command.change_model.model),
                )
                operation = session_dispatcher_pb2.OPERATION_CHANGE_MODEL
            case "change_reasoning_effort":
                response = await runner.control.runner_unary(
                    runner.destination,
                    lambda stub: stub.ChangeReasoningEffort,
                    runner_control_pb2.ChangeReasoningEffortRequest(
                        **common, effort=command.change_reasoning_effort.effort
                    ),
                )
                operation = session_dispatcher_pb2.OPERATION_CHANGE_REASONING_EFFORT
            case "stop_runner_session":
                response = await runner.control.runner_unary(
                    runner.destination,
                    lambda stub: stub.StopRunnerSession,
                    runner_control_pb2.StopRunnerSessionRequest(**common),
                )
                operation = session_dispatcher_pb2.OPERATION_STOP_RUNNER_SESSION
            case "interrupt_turn":
                response = await runner.control.runner_unary(
                    runner.destination,
                    lambda stub: stub.InterruptTurn,
                    runner_control_pb2.InterruptTurnRequest(**common, turn_id=command.interrupt_turn.turn_id),
                )
                # Interrupts are delivered immediately and are deliberately absent from the
                # durable Dispatch operation enum. Mirror SessionDispatcherClient's transient
                # response conversion while retaining the runner admission receipt.
            case _:
                raise ValueError(
                    f"unsupported Session Dispatcher command operation: {command.WhichOneof('operation')!r}"
                )
        admission = response.admission
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
        created = await runner.open_session(session_id=UUID(session_id), spec=spec, setup_script=setup_script)
        await self.event_logs.open(created.session_id)
        opening = session_dispatcher_pb2.SessionOpening(
            session_id=created.session_id, sandbox=sandbox, state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
        )
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
