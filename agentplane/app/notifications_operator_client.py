"""The Notification Service's operator watches, as the SSE `snapshot` frames the browser follows."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Collection
from pathlib import Path
from typing import Any
from uuid import UUID

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from agentplane.grpc.calls import bearer, raise_status
from agentplane.notification_service import notifications_operator_pb2 as pb, notifications_operator_pb2_grpc as rpc
from agentplane.sse import frame

# Generated messages use protobuf field-presence stubs.
# gazelle:include_dep @pypi//protobuf


class NotificationsOperatorClient:
    def __init__(self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float) -> None:
        self._stub = rpc.NotificationsOperatorStub(channel)
        self._token_file = token_file
        self._request_timeout_s = request_timeout_s

    def watch_sandbox_notifications(self, *, namespace: str, name: str, uid: UUID) -> AsyncGenerator[bytes]:
        """`snapshot` frames of the Sandbox incarnation's notification state until the service plans a renewal.

        Anything else ends it with an exception, bare EOF included.
        """
        return self._snapshots(
            self._stub.WatchSandboxNotifications,
            pb.WatchSandboxNotificationsRequest(namespace=namespace, name=name, uid=str(uid)),
        )

    def watch_session_notices(self, session_ids: Collection[UUID]) -> AsyncGenerator[bytes]:
        """`snapshot` frames of the named Sessions' notice states, ended as `watch_sandbox_notifications` is."""
        return self._snapshots(
            self._stub.WatchSessionNotices,
            pb.WatchSessionNoticesRequest(session_ids=[str(session_id) for session_id in session_ids]),
        )

    async def _snapshots[Response: (pb.WatchSandboxNotificationsResponse, pb.WatchSessionNoticesResponse)](
        self, watch: Callable[..., grpc.aio.UnaryStreamCall[Any, Response]], request: Message
    ) -> AsyncGenerator[bytes]:
        call = watch(request, metadata=await bearer(self._token_file))
        # A long-lived stream: no call deadline. The service's opening snapshot bounds the handshake,
        # and its lease ends the stream with reconnect_required.
        handshake: float | None = self._request_timeout_s
        try:
            while True:
                try:
                    async with asyncio.timeout(handshake):
                        message = await call.read()
                except grpc.aio.AioRpcError as error:
                    raise_status(error)
                handshake = None
                if message is grpc.aio.EOF:
                    raise ConnectionError("Notification Service ended the watch without planned renewal")
                assert isinstance(message, (pb.WatchSandboxNotificationsResponse, pb.WatchSessionNoticesResponse))
                if message.HasField("reconnect_required"):
                    return
                yield frame("snapshot", MessageToDict(message.snapshot, always_print_fields_with_no_presence=True))
        finally:
            call.cancel()
