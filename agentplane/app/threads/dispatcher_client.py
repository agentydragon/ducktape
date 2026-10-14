"""Authenticated Thread command calls to the durable Session Dispatcher."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.grpc_calls import call_unary
from agentplane.proto import command_pb2, session_dispatcher_pb2, session_dispatcher_pb2_grpc

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf


class SessionDispatcherClient:
    def __init__(self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float) -> None:
        self.stub: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub = (
            session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        )
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s

    async def command(
        self, *, sandbox: str, session_id: str, command: command_pb2.Command
    ) -> session_dispatcher_pb2.Dispatch:
        """Queue one durable command, or deliver an interrupt immediately as a transient result."""
        operation = command.WhichOneof("operation")
        common = {"sandbox": sandbox, "session_id": session_id, "command_id": command.command_id}
        match operation:
            case "submit_input":
                response = await self._unary(
                    self.stub.SubmitInput,
                    session_dispatcher_pb2.SubmitInputRequest(**common, text=command.submit_input.text),
                )
                return response.dispatch
            case "change_model":
                response = await self._unary(
                    self.stub.ChangeModel,
                    session_dispatcher_pb2.ChangeModelRequest(**common, model=command.change_model.model),
                )
                return response.dispatch
            case "change_reasoning_effort":
                response = await self._unary(
                    self.stub.ChangeReasoningEffort,
                    session_dispatcher_pb2.ChangeReasoningEffortRequest(
                        **common, effort=command.change_reasoning_effort.effort
                    ),
                )
                return response.dispatch
            case "stop_runner_session":
                response = await self._unary(
                    self.stub.StopRunnerSession, session_dispatcher_pb2.StopRunnerSessionRequest(**common)
                )
                return response.dispatch
            case "interrupt_turn":
                response = await self._unary(
                    self.stub.InterruptTurn,
                    session_dispatcher_pb2.InterruptTurnRequest(**common, turn_id=command.interrupt_turn.turn_id),
                )
                dispatch = session_dispatcher_pb2.Dispatch(
                    command_id=command.command_id, session_id=session_id, sandbox=sandbox, state=response.state
                )
                if response.HasField("reason"):
                    dispatch.reason = response.reason
                if response.HasField("admission"):
                    dispatch.admission.CopyFrom(response.admission)
                return dispatch
            case _:
                raise ValueError(f"unsupported Session Dispatcher command operation: {operation!r}")

    async def get_dispatch(self, command_id: str) -> session_dispatcher_pb2.Dispatch:
        response = await self._unary(
            self.stub.GetDispatch, session_dispatcher_pb2.GetDispatchRequest(command_id=command_id)
        )
        return response.dispatch

    async def _unary[ResponseT: Message](
        self, call: Callable[..., Awaitable[ResponseT]], request: Message
    ) -> ResponseT:
        return await call_unary(call, request, token_file=self.token_file, timeout_s=self.request_timeout_s)
