"""A scripted ActionService for app BFF and client tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.message import Message

from agentplane.action_service import action_service_pb2, action_service_pb2_grpc
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving


@dataclass(frozen=True)
class ActionCall:
    method: str
    request: Message
    bearer_token: str | None


def _bearer(context: grpc.aio.ServicerContext) -> str | None:
    return next((str(value) for key, value in context.invocation_metadata() or () if key == "authorization"), None)


class FakeActionService(action_service_pb2_grpc.ActionServiceServicer):
    """A minimal ActionService whose calls and watch lifecycle tests can control."""

    def __init__(self) -> None:
        self.calls: list[ActionCall] = []
        self.policy = action_service_pb2.GetActionPolicyResponse()
        self.requests = action_service_pb2.ListActionRequestsResponse()
        self.groups = action_service_pb2.ListActionGroupsResponse()
        self.unary_gate: asyncio.Event | None = None
        self.unary_started = asyncio.Event()
        self.watch_gate: asyncio.Event | None = None
        self.watch_started = asyncio.Event()
        self.watch_cancelled = asyncio.Event()
        self.watch_error_before_first: grpc.StatusCode | None = None
        self.watch_first = action_service_pb2.WatchActionRequestsResponse(snapshot_complete=Empty())
        self.watch_frames: asyncio.Queue[action_service_pb2.WatchActionRequestsResponse | grpc.StatusCode | None] = (
            asyncio.Queue()
        )

    def _record(self, method: str, request: Message, context: grpc.aio.ServicerContext) -> None:
        self.calls.append(ActionCall(method, request, _bearer(context)))

    async def _hold_unary(self) -> None:
        if self.unary_gate is not None:
            self.unary_started.set()
            await self.unary_gate.wait()

    @override
    async def GetActionPolicy(
        self, request: action_service_pb2.GetActionPolicyRequest, context: grpc.aio.ServicerContext
    ) -> action_service_pb2.GetActionPolicyResponse:
        self._record("GetActionPolicy", request, context)
        await self._hold_unary()
        return self.policy

    @override
    async def ListActionRequests(
        self, request: action_service_pb2.ListActionRequestsRequest, context: grpc.aio.ServicerContext
    ) -> action_service_pb2.ListActionRequestsResponse:
        self._record("ListActionRequests", request, context)
        await self._hold_unary()
        return self.requests

    @override
    async def ListActionGroups(
        self, request: action_service_pb2.ListActionGroupsRequest, context: grpc.aio.ServicerContext
    ) -> action_service_pb2.ListActionGroupsResponse:
        self._record("ListActionGroups", request, context)
        await self._hold_unary()
        return self.groups

    @override
    async def WatchActionRequests(
        self, request: action_service_pb2.WatchActionRequestsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[action_service_pb2.WatchActionRequestsResponse]:
        self._record("WatchActionRequests", request, context)
        self.watch_started.set()
        try:
            if self.watch_gate is not None:
                await self.watch_gate.wait()
            if self.watch_error_before_first is not None:
                await context.abort(self.watch_error_before_first, "scripted Action Service failure")
            yield self.watch_first
            while True:
                step = await self.watch_frames.get()
                if step is None:
                    return
                if isinstance(step, grpc.StatusCode):
                    await context.abort(step, "scripted Action Service failure")
                yield step
        except asyncio.CancelledError:
            self.watch_cancelled.set()
            raise


@asynccontextmanager
async def served_action_service(
    service: FakeActionService, *, request_timeout_s: float = 5
) -> AsyncIterator[tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient]]:
    """Serve the fake on loopback and yield its generated stub with the shared transport."""
    server = grpc.aio.server()
    action_service_pb2_grpc.add_ActionServiceServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield (
            service,
            action_service_pb2_grpc.ActionServiceStub(channel),
            OperatorGrpcClient(request_timeout_s=request_timeout_s),
        )
