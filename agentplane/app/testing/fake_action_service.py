"""A scripted ActionService for app BFF and client tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc
from google.protobuf.message import Message

from agentplane.action_service.api.contracts import action_service_pb2, action_service_pb2_grpc
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
        self.policy_watch_frames: asyncio.Queue[action_service_pb2.GetActionPolicyResponse] = asyncio.Queue()
        self.policy_watch_cancelled = asyncio.Event()
        self.requests = action_service_pb2.ListActionRequestsResponse()

    def _record(self, method: str, request: Message, context: grpc.aio.ServicerContext) -> None:
        self.calls.append(ActionCall(method, request, _bearer(context)))

    @override
    async def GetActionPolicy(
        self, request: action_service_pb2.GetActionPolicyRequest, context: grpc.aio.ServicerContext
    ) -> action_service_pb2.GetActionPolicyResponse:
        self._record("GetActionPolicy", request, context)
        return self.policy

    @override
    async def WatchActionPolicy(
        self, request: action_service_pb2.GetActionPolicyRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[action_service_pb2.GetActionPolicyResponse]:
        self._record("WatchActionPolicy", request, context)
        initial = action_service_pb2.GetActionPolicyResponse()
        initial.CopyFrom(self.policy)
        initial.action_policy.subject.CopyFrom(request.subject)
        try:
            yield initial
            while True:
                yield await self.policy_watch_frames.get()
        except asyncio.CancelledError:
            self.policy_watch_cancelled.set()
            raise

    @override
    async def ListActionRequests(
        self, request: action_service_pb2.ListActionRequestsRequest, context: grpc.aio.ServicerContext
    ) -> action_service_pb2.ListActionRequestsResponse:
        self._record("ListActionRequests", request, context)
        return self.requests


@asynccontextmanager
async def served_action_service(service: FakeActionService) -> AsyncIterator[tuple[FakeActionService, str]]:
    """Serve the fake on loopback and yield its gRPC target for the real app process."""
    server = grpc.aio.server()
    action_service_pb2_grpc.add_ActionServiceServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield service, f"127.0.0.1:{port}"
