"""A Runner Gateway servicer whose every RPC answers UNIMPLEMENTED."""

from collections.abc import AsyncIterator

import grpc

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner_gateway import protocol_pb2_grpc
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


class UnimplementedRunnerGateway(protocol_pb2_grpc.RunnerGatewayServicer):
    """The generated stub marks each RPC abstract, so a fake subclasses this and overrides only the
    RPCs its test drives."""

    async def ListSessions(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ListSessions")

    async def CreateSession(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CreateSessionResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve CreateSession")

    async def LookupSession(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.LookupSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.LookupSessionResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve LookupSession")

    async def ResumeSession(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.SessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ResumeSession")

    async def SubmitCommand(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve SubmitCommand")

    async def GetCommand(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.GetCommandRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CommandStatus:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve GetCommand")

    async def ListCommands(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.ListCommandsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListCommandsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ListCommands")

    async def FollowSession(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve FollowSession")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    async def WatchSessions(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.WatchSessionsResponse]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve WatchSessions")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    async def SealSandbox(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.SealResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve SealSandbox")
