"""Authenticated calls to the durable Session Dispatcher."""

from pathlib import Path

import grpc

from agentplane.grpc_calls import bearer, raise_status
from agentplane.proto import session_dispatcher_pb2, session_dispatcher_pb2_grpc

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf


class SessionDispatcherClient:
    def __init__(self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float) -> None:
        self.stub: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub = (
            session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        )
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s

    async def submit_input(
        self, *, sandbox: str, session_id: str, command_id: str, text: str
    ) -> session_dispatcher_pb2.Dispatch:
        """Store a notice using its durable command ID. A retry returns the same Dispatch."""
        try:
            response = await self.stub.SubmitInput(
                session_dispatcher_pb2.SubmitInputRequest(
                    sandbox=sandbox, session_id=session_id, command_id=command_id, text=text
                ),
                metadata=await bearer(self.token_file),
                timeout=self.request_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            raise_status(error)
        return response.dispatch
