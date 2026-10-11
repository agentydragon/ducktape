"""A Sandbox Service servicer whose every RPC answers UNIMPLEMENTED."""

from typing import override

import grpc
from google.protobuf.empty_pb2 import Empty

from agentplane.sandbox_service import protocol_pb2, protocol_pb2_grpc

# gazelle:include_dep @pypi//protobuf


class UnimplementedSandboxService(protocol_pb2_grpc.SandboxServiceServicer):
    """The generated stub marks each RPC abstract, so a fake subclasses this and overrides only the
    RPCs its test drives."""

    @override
    async def ListSandboxes(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListSandboxesResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ListSandboxes")

    @override
    async def GetSandbox(
        self, request: protocol_pb2.GetSandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Sandbox:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve GetSandbox")

    @override
    async def CreateSandbox(
        self, request: protocol_pb2.CreateSandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Sandbox:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve CreateSandbox")

    @override
    async def SuspendSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve SuspendSandbox")

    @override
    async def ResumeSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ResumeSandbox")

    @override
    async def DeleteSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve DeleteSandbox")

    @override
    async def ListTemplates(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListTemplatesResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ListTemplates")

    @override
    async def GrantEgress(
        self, request: protocol_pb2.GrantEgressRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.GrantEgressResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve GrantEgress")

    @override
    async def RevokeEgress(self, request: protocol_pb2.RevokeEgressRequest, context: grpc.aio.ServicerContext) -> Empty:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve RevokeEgress")

    @override
    async def PlaceHold(
        self, request: protocol_pb2.HoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve PlaceHold")

    @override
    async def ConfirmHold(
        self, request: protocol_pb2.ConfirmHoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ConfirmHold")

    @override
    async def ReleaseHold(self, request: protocol_pb2.HoldRequest, context: grpc.aio.ServicerContext) -> Empty:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ReleaseHold")

    @override
    async def ListHolds(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListHoldsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake Sandbox Service does not serve ListHolds")
