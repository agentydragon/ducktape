"""A Runner Gateway servicer whose every RPC answers UNIMPLEMENTED."""

from collections.abc import AsyncIterator
from typing import override

import grpc

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner_gateway import protocol_pb2_grpc
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


class UnimplementedRunnerGateway(protocol_pb2_grpc.RunnerGatewayServicer):
    """The generated stub marks each RPC abstract, so a fake subclasses this and overrides only the
    RPCs its test drives."""

    @override
    async def ListSessions(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ListSessions")

    @override
    async def CreateSession(
        self, request: protocol_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CreateSessionResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve CreateSession")

    @override
    async def LookupSession(
        self, request: protocol_pb2.LookupSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.LookupSessionResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve LookupSession")

    @override
    async def ResumeSession(
        self, request: protocol_pb2.SessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ResumeSession")

    @override
    async def SubmitCommand(
        self, request: protocol_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve SubmitCommand")

    @override
    async def GetCommand(
        self, request: protocol_pb2.GetCommandRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CommandStatus:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve GetCommand")

    @override
    async def ListCommands(
        self, request: protocol_pb2.ListCommandsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListCommandsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve ListCommands")

    @override
    async def FollowSession(
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve FollowSession")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    @override
    async def WatchSessions(
        self, request: protocol_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.WatchSessionsResponse]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve WatchSessions")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    @override
    async def SealSandbox(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.SealResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake gateway does not serve SealSandbox")
