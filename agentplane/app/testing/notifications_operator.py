"""A gRPC stand-in for the Notification Service's operator API."""

import asyncio
from collections.abc import AsyncIterator
from typing import override

import grpc

from agentplane.notification_service import notifications_operator_pb2 as pb, notifications_operator_pb2_grpc as rpc

# Generated messages use protobuf field-presence stubs.
# gazelle:include_dep @pypi//protobuf


async def _play[Frame: (pb.WatchSandboxNotificationsResponse, pb.WatchSessionNoticesResponse)](
    script: asyncio.Queue[Frame | grpc.StatusCode | None], context: grpc.aio.ServicerContext
) -> AsyncIterator[Frame]:
    """Each scripted frame in turn; a status aborts the call with it, and None is a bare EOF.

    The call ends after `reconnect_required`, as the service's does: a script left in the queue is the next
    call's, never a finished one's.
    """
    while (step := await script.get()) is not None:
        if isinstance(step, grpc.StatusCode):
            await context.abort(step, "scripted failure")
        else:
            yield step
            if step.HasField("reconnect_required"):
                return


def _bearer(context: grpc.aio.ServicerContext) -> str | None:
    return next((str(value) for key, value in context.invocation_metadata() or () if key == "authorization"), None)


class FakeNotificationsOperator(rpc.NotificationsOperatorServicer):
    """Plays whatever a test puts in `sandbox_script` and `session_script`, one call after another.

    `hung_up` is set when the caller cancels a watch.
    """

    def __init__(self) -> None:
        self.sandbox_script: asyncio.Queue[pb.WatchSandboxNotificationsResponse | grpc.StatusCode | None] = (
            asyncio.Queue()
        )
        self.session_script: asyncio.Queue[pb.WatchSessionNoticesResponse | grpc.StatusCode | None] = asyncio.Queue()
        self.sandbox_requests: list[pb.WatchSandboxNotificationsRequest] = []
        self.session_requests: list[pb.WatchSessionNoticesRequest] = []
        self.tokens: list[str | None] = []
        self.hung_up = asyncio.Event()

    @override
    async def WatchSandboxNotifications(
        self, request: pb.WatchSandboxNotificationsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.WatchSandboxNotificationsResponse]:
        self.sandbox_requests.append(request)
        self.tokens.append(_bearer(context))
        try:
            async for response in _play(self.sandbox_script, context):
                yield response
        except asyncio.CancelledError:
            self.hung_up.set()
            raise

    @override
    async def WatchSessionNotices(
        self, request: pb.WatchSessionNoticesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.WatchSessionNoticesResponse]:
        self.session_requests.append(request)
        self.tokens.append(_bearer(context))
        try:
            async for response in _play(self.session_script, context):
                yield response
        except asyncio.CancelledError:
            self.hung_up.set()
            raise
