"""Authenticated gRPC commands to Sessions, queued durably and delivered by `Delivery`."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc
from google.protobuf.message import Error as ProtobufError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_servicer import Abort, ServicerResources, status_mapping
from agentplane.proto import command_pb2, session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch
from agentplane.session_dispatcher.models import DeliveryState, Operation, Reason
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch, delivery_blocker
from agentplane.session_dispatcher.store import (
    CommandConflictError,
    DispatchNotFoundError,
    DispatchStore,
    NotCancellableError,
    QueueFullError,
)

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)

_OPERATIONS = {
    Operation.SUBMIT_INPUT: session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
    Operation.CHANGE_MODEL: session_dispatcher_pb2.OPERATION_CHANGE_MODEL,
    Operation.CHANGE_REASONING_EFFORT: session_dispatcher_pb2.OPERATION_CHANGE_REASONING_EFFORT,
    Operation.STOP_RUNNER_SESSION: session_dispatcher_pb2.OPERATION_STOP_RUNNER_SESSION,
}
_STATES = {
    DeliveryState.QUEUED_IN_DISPATCHER: session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
    DeliveryState.DELIVERING_TO_RUNNER: session_dispatcher_pb2.DELIVERY_STATE_DELIVERING_TO_RUNNER,
    DeliveryState.RUNNER_ADMITTED: session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
    DeliveryState.HARNESS_CONFIRMED: session_dispatcher_pb2.DELIVERY_STATE_HARNESS_CONFIRMED,
    DeliveryState.NO_OP: session_dispatcher_pb2.DELIVERY_STATE_NO_OP,
    DeliveryState.FAILED: session_dispatcher_pb2.DELIVERY_STATE_FAILED,
    DeliveryState.CANCELLED_IN_DISPATCHER: session_dispatcher_pb2.DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
    DeliveryState.UNKNOWN: session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
}


@dataclass(frozen=True)
class Resources(ServicerResources):
    store: DispatchStore
    sandboxes: SandboxWatch
    runner: RunnerClient
    request_timeout_s: float
    # How long one WatchDispatches stream lasts before the caller reconnects.
    watch_lease_s: float


# No bearer, database or cluster detail reaches clients.
errors = status_mapping(
    Abort(CommandConflictError, (grpc.StatusCode.ALREADY_EXISTS, "command_id was used for other work")),
    Abort(QueueFullError, lambda error: (grpc.StatusCode.RESOURCE_EXHAUSTED, str(error))),
    Abort(DispatchNotFoundError, (grpc.StatusCode.NOT_FOUND, "dispatch not found")),
    Abort(NotCancellableError, (grpc.StatusCode.FAILED_PRECONDITION, "delivery already began")),
    Abort(grpc.aio.AioRpcError, lambda error: (error.code(), f"runner: {error.details()}")),
    Abort((ValueError, ProtobufError), (grpc.StatusCode.INVALID_ARGUMENT, "invalid dispatcher request")),
    deadline="dispatcher deadline expired",
    unavailable="dispatcher storage or authentication unavailable",
    also_unavailable=(SQLAlchemyError,),
)


def describe_dispatch(row: Dispatch) -> session_dispatcher_pb2.Dispatch:
    dispatch = session_dispatcher_pb2.Dispatch(
        command_id=row.command_id,
        session_id=row.session_id,
        sandbox=row.sandbox_name,
        sandbox_uid=None if row.sandbox_uid is None else str(row.sandbox_uid),
        operation=_OPERATIONS[row.operation],
        state=_STATES[row.state],
        reason=row.reason,
    )
    if row.admission is not None:
        dispatch.admission.CopyFrom(row.admission)
    dispatch.created_at.FromDatetime(row.created_at)
    dispatch.updated_at.FromDatetime(row.updated_at)
    return dispatch


def _require(**fields: str) -> None:
    for name, value in fields.items():
        if not value:
            raise ValueError(f"{name} is required")


class SessionDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @asynccontextmanager
    async def request(self, context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            yield

    async def _enqueue(
        self, *, command_id: str, session_id: str, sandbox: str, command: command_pb2.Command
    ) -> session_dispatcher_pb2.Dispatch:
        _require(command_id=command_id, session_id=session_id, sandbox=sandbox)
        current = self.resources.sandboxes.get_sandbox(sandbox)
        return describe_dispatch(
            await self.resources.store.enqueue(
                session_id=session_id,
                sandbox_name=sandbox,
                sandbox_uid=None if current is None else current.metadata.uid,
                command=command,
            )
        )

    @override
    async def SubmitInput(
        self, request: session_dispatcher_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.SubmitInputResponse:
        async with self.request(context):
            return session_dispatcher_pb2.SubmitInputResponse(
                dispatch=await self._enqueue(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    command=command_pb2.Command(
                        command_id=request.command_id, submit_input=command_pb2.SubmitInput(text=request.text)
                    ),
                )
            )

    @override
    async def ChangeModel(
        self, request: session_dispatcher_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.ChangeModelResponse:
        async with self.request(context):
            return session_dispatcher_pb2.ChangeModelResponse(
                dispatch=await self._enqueue(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    command=command_pb2.Command(
                        command_id=request.command_id, change_model=command_pb2.ChangeModel(model=request.model)
                    ),
                )
            )

    @override
    async def ChangeReasoningEffort(
        self, request: session_dispatcher_pb2.ChangeReasoningEffortRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.ChangeReasoningEffortResponse:
        async with self.request(context):
            return session_dispatcher_pb2.ChangeReasoningEffortResponse(
                dispatch=await self._enqueue(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    command=command_pb2.Command(
                        command_id=request.command_id,
                        change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=request.effort),
                    ),
                )
            )

    @override
    async def StopRunnerSession(
        self, request: session_dispatcher_pb2.StopRunnerSessionRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.StopRunnerSessionResponse:
        async with self.request(context):
            return session_dispatcher_pb2.StopRunnerSessionResponse(
                dispatch=await self._enqueue(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    command=command_pb2.Command(
                        command_id=request.command_id, stop_runner_session=command_pb2.StopRunnerSession()
                    ),
                )
            )

    @override
    async def InterruptTurn(
        self, request: session_dispatcher_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.InterruptTurnResponse:
        async with self.request(context):
            _require(command_id=request.command_id, session_id=request.session_id, sandbox=request.sandbox)
            sandbox = self.resources.sandboxes.get_sandbox(request.sandbox)
            if sandbox is None:
                return session_dispatcher_pb2.InterruptTurnResponse(
                    state=_STATES[DeliveryState.NO_OP], reason=Reason.SANDBOX_DELETED
                )
            if (blocker := delivery_blocker(sandbox)) is not None:
                return session_dispatcher_pb2.InterruptTurnResponse(state=_STATES[DeliveryState.NO_OP], reason=blocker)
            try:
                admission = await self.resources.runner.deliver(
                    request.sandbox,
                    request.session_id,
                    command_pb2.Command(
                        command_id=request.command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=request.turn_id)
                    ),
                )
            except grpc.aio.AioRpcError as error:
                if error.code() is not grpc.StatusCode.FAILED_PRECONDITION:
                    raise
                return session_dispatcher_pb2.InterruptTurnResponse(
                    state=_STATES[DeliveryState.NO_OP], reason=Reason.NOTHING_RUNNING
                )
            return session_dispatcher_pb2.InterruptTurnResponse(
                state=_STATES[DeliveryState.RUNNER_ADMITTED], admission=admission
            )

    @override
    async def GetDispatch(
        self, request: session_dispatcher_pb2.GetDispatchRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.GetDispatchResponse:
        async with self.request(context):
            _require(command_id=request.command_id)
            return session_dispatcher_pb2.GetDispatchResponse(
                dispatch=describe_dispatch(await self.resources.store.get_dispatch(request.command_id))
            )

    @override
    async def ListDispatches(
        self, request: session_dispatcher_pb2.ListDispatchesRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.ListDispatchesResponse:
        async with self.request(context):
            return session_dispatcher_pb2.ListDispatchesResponse(
                dispatches=[describe_dispatch(row) for row in await self._list(request.filter)]
            )

    @override
    async def CancelDispatch(
        self, request: session_dispatcher_pb2.CancelDispatchRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.CancelDispatchResponse:
        async with self.request(context):
            _require(command_id=request.command_id)
            return session_dispatcher_pb2.CancelDispatchResponse(
                dispatch=describe_dispatch(await self.resources.store.cancel(request.command_id))
            )

    @override
    async def WatchDispatches(  # type: ignore[override]
        self, request: session_dispatcher_pb2.WatchDispatchesRequest, context: grpc.aio.ServicerContext
    ) -> None:
        """Write every Dispatch of the scope on open and each one again after it changes, until the lease ends."""
        async with errors(context):
            async with asyncio.timeout(self.resources.request_timeout_s):
                await self.resources.authenticate(context)
            deadline = asyncio.get_running_loop().time() + self.resources.watch_lease_s
            written: dict[str, tuple[DeliveryState, str | None]] = {}
            changed = asyncio.Event()
            # Subscribed before the first read, so a commit after it is never missed.
            with self.resources.store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
                while True:
                    changed.clear()
                    async with asyncio.timeout(self.resources.request_timeout_s):
                        rows = await self._list(request.filter)
                    for row in rows:
                        if written.get(row.command_id) == (row.state, row.reason):
                            continue
                        async with asyncio.timeout(self.resources.request_timeout_s):
                            await context.write(
                                session_dispatcher_pb2.WatchDispatchesResponse(dispatch=describe_dispatch(row))
                            )
                        written[row.command_id] = (row.state, row.reason)
                    lease = asyncio.timeout_at(deadline)
                    try:
                        async with lease:
                            await changed.wait()
                    except TimeoutError:
                        if not lease.expired():
                            raise
                        return

    async def _list(self, scope: session_dispatcher_pb2.DispatchFilter) -> list[Dispatch]:
        match scope.WhichOneof("scope"):
            case "session_id":
                return await self.resources.store.list_dispatches(session_id=scope.session_id)
            case "sandbox":
                return await self.resources.store.list_dispatches(sandbox_name=scope.sandbox)
            case _:
                raise ValueError("a filter naming a session or a sandbox is required")


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(SessionDispatcher(resources), server)
