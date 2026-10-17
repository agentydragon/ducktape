"""A generated Dispatcher servicer backed by a real local Runner proxy for app tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override
from uuid import UUID

import grpc
from google.protobuf.message import Message

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.grpc.calls import RefusedError, ServiceError, dial
from agentplane.grpc.serving import serving
from agentplane.proto import runner_control_pb2, session_dispatcher_pb2, session_dispatcher_pb2_grpc


class RunnerProxyBackedDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    """Exercise generated RPCs while keeping runner-backed app scenarios local.

    This fixture acknowledges commands immediately through the Runner proxy; it is not a
    durable queue test. Dispatcher queue and retry semantics have separate service tests.
    """

    def __init__(self, runners: RunnerDirectory, event_logs: EventLogStore) -> None:
        self.runners = runners
        self.event_logs = event_logs
        self.openings: dict[str, session_dispatcher_pb2.SessionOpening] = {}

    async def _deliver(
        self,
        request: object,
        context: grpc.aio.ServicerContext,
        rpc: str,
        runner_request: Message,
        operation: session_dispatcher_pb2.Operation,
    ) -> session_dispatcher_pb2.Dispatch:
        sandbox = request.sandbox  # type: ignore[attr-defined]
        runner = self.runners.client(sandbox)
        try:
            response = await runner.control.runner_unary(
                runner.destination, lambda stub: getattr(stub, rpc), runner_request
            )
        except ServiceError as error:
            await context.abort(error.code, error.details or str(error))
        except RefusedError as error:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
        return session_dispatcher_pb2.Dispatch(
            command_id=request.command_id,  # type: ignore[attr-defined]
            session_id=request.session_id,  # type: ignore[attr-defined]
            sandbox=sandbox,
            sandbox_uid=runner.destination.sandbox_uid,
            operation=operation,
            state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
            admission=response.admission,
        )

    @override
    async def SubmitInput(self, request, context):
        dispatch = await self._deliver(
            request,
            context,
            "SubmitInput",
            runner_control_pb2.SubmitInputRequest(
                session_id=request.session_id, command_id=request.command_id, text=request.text
            ),
            session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
        )
        return session_dispatcher_pb2.SubmitInputResponse(dispatch=dispatch)

    @override
    async def ChangeModel(self, request, context):
        dispatch = await self._deliver(
            request,
            context,
            "ChangeModel",
            runner_control_pb2.ChangeModelRequest(
                session_id=request.session_id, command_id=request.command_id, model=request.model
            ),
            session_dispatcher_pb2.OPERATION_CHANGE_MODEL,
        )
        return session_dispatcher_pb2.ChangeModelResponse(dispatch=dispatch)

    @override
    async def ChangeReasoningEffort(self, request, context):
        dispatch = await self._deliver(
            request,
            context,
            "ChangeReasoningEffort",
            runner_control_pb2.ChangeReasoningEffortRequest(
                session_id=request.session_id, command_id=request.command_id, effort=request.effort
            ),
            session_dispatcher_pb2.OPERATION_CHANGE_REASONING_EFFORT,
        )
        return session_dispatcher_pb2.ChangeReasoningEffortResponse(dispatch=dispatch)

    @override
    async def StopRunnerSession(self, request, context):
        dispatch = await self._deliver(
            request,
            context,
            "StopRunnerSession",
            runner_control_pb2.StopRunnerSessionRequest(session_id=request.session_id, command_id=request.command_id),
            session_dispatcher_pb2.OPERATION_STOP_RUNNER_SESSION,
        )
        return session_dispatcher_pb2.StopRunnerSessionResponse(dispatch=dispatch)

    @override
    async def InterruptTurn(self, request, context):
        runner = self.runners.client(request.sandbox)
        try:
            response = await runner.control.runner_unary(
                runner.destination,
                lambda stub: stub.InterruptTurn,
                runner_control_pb2.InterruptTurnRequest(
                    session_id=request.session_id, command_id=request.command_id, turn_id=request.turn_id
                ),
            )
        except ServiceError as error:
            await context.abort(error.code, error.details or str(error))
        except RefusedError as error:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
        return session_dispatcher_pb2.InterruptTurnResponse(
            state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, admission=response.admission
        )

    @override
    async def OpenSession(self, request, context):
        runner = self.runners.client(request.sandbox)
        created = await runner.open_session(
            session_id=UUID(request.session_id),
            spec=request.spec,
            setup_script=request.setup_script if request.HasField("setup_script") else None,
        )
        await self.event_logs.open(created.session_id)
        opening = session_dispatcher_pb2.SessionOpening(
            session_id=created.session_id,
            sandbox=request.sandbox,
            state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
        )
        self.openings[created.session_id] = opening
        return session_dispatcher_pb2.OpenSessionResponse(opening=opening)

    @override
    async def GetSessionOpening(self, request, context):
        opening = self.openings.get(request.session_id)
        if opening is None:
            opening = session_dispatcher_pb2.SessionOpening(
                session_id=request.session_id, state=session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN
            )
        return session_dispatcher_pb2.GetSessionOpeningResponse(opening=opening)


@asynccontextmanager
async def runner_proxy_backed_dispatcher(
    runners: RunnerDirectory, event_logs: EventLogStore, token_file: Path
) -> AsyncIterator[SessionDispatcherClient]:
    """Serve the generated API over a real channel for app integration tests."""
    await asyncio.to_thread(token_file.write_text, "test-dispatcher-token")
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(
        RunnerProxyBackedDispatcher(runners, event_logs), server
    )
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=10)
