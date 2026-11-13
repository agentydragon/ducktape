"""Scripted gRPC Notification Service for app integration tests."""

import asyncio
from collections.abc import AsyncIterator
from typing import override

import grpc

from agentplane.notification_service.api.notifications import notifications_pb2 as pb, notifications_pb2_grpc as rpc

# Generated messages use protobuf field-presence stubs.
# gazelle:include_dep @pypi//protobuf


class FakeNotifications(rpc.NotificationsServicer):
    def __init__(self) -> None:
        self.inbox_script: asyncio.Queue[pb.WatchInboxResponse | grpc.StatusCode | None] = asyncio.Queue()
        self.requests: list[pb.WatchInboxRequest] = []
        self.tokens: list[str | None] = []
        self.hung_up = asyncio.Event()

    @override
    async def WatchInbox(
        self, request: pb.WatchInboxRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.WatchInboxResponse]:
        self.requests.append(request)
        self.tokens.append(next((str(v) for k, v in context.invocation_metadata() or () if k == "authorization"), None))
        try:
            while (step := await self.inbox_script.get()) is not None:
                if isinstance(step, grpc.StatusCode):
                    await context.abort(step, "scripted failure")
                else:
                    yield step
        except asyncio.CancelledError:
            self.hung_up.set()
            raise
