"""Native Dispatcher transport works without an integration app or browser session."""

from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError, dial
from agentplane.grpc.serving import serving
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient


class Dispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.requests: list[session_dispatcher_pb2.SubmitInputRequest] = []
        self.bearers: list[str | bytes | None] = []
        self.failure: grpc.StatusCode | None = None

    @override
    async def SubmitInput(
        self,
        request: session_dispatcher_pb2.SubmitInputRequest,
        context: grpc.aio.ServicerContext[
            session_dispatcher_pb2.SubmitInputRequest, session_dispatcher_pb2.SubmitInputResponse
        ],
    ) -> session_dispatcher_pb2.SubmitInputResponse:
        self.requests.append(request)
        self.bearers.append(dict(context.invocation_metadata()).get("authorization"))
        if self.failure is not None:
            await context.abort(self.failure, "submission outcome unknown")
        return session_dispatcher_pb2.SubmitInputResponse(
            dispatch=session_dispatcher_pb2.Dispatch(
                command_id=request.command_id,
                session_id=request.session_id,
                sandbox=request.sandbox,
                sandbox_uid=request.sandbox_uid,
                state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
            )
        )

    @override
    async def GetSessionOpening(
        self,
        request: session_dispatcher_pb2.GetSessionOpeningRequest,
        context: grpc.aio.ServicerContext[
            session_dispatcher_pb2.GetSessionOpeningRequest, session_dispatcher_pb2.GetSessionOpeningResponse
        ],
    ) -> session_dispatcher_pb2.GetSessionOpeningResponse:
        self.bearers.append(dict(context.invocation_metadata()).get("authorization"))
        return session_dispatcher_pb2.GetSessionOpeningResponse(
            opening=session_dispatcher_pb2.SessionOpening(
                session_id=request.session_id, sandbox="original", sandbox_uid="original-uid"
            )
        )


async def test_native_submission_preserves_identity_and_rotates_service_credentials(tmp_path: Path) -> None:
    backend = Dispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("first-service-token")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        client = SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=5)
        sandbox, uid = await client.session_target("session")
        first = await client.submit_input("session", "same-command", sandbox, uid, "original input")
        token_file.write_text("rotated-service-token")
        retry = await client.submit_input("session", "same-command", sandbox, uid, "original input")
    assert first == retry
    assert first.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    assert (
        backend.requests
        == [
            session_dispatcher_pb2.SubmitInputRequest(
                session_id="session",
                command_id="same-command",
                sandbox="original",
                sandbox_uid="original-uid",
                text="original input",
            )
        ]
        * 2
    )
    assert backend.bearers == [
        "Bearer first-service-token",
        "Bearer first-service-token",
        "Bearer rotated-service-token",
    ]


async def test_native_submission_does_not_automatically_retry_an_unknown_outcome(tmp_path: Path) -> None:
    backend = Dispatcher()
    backend.failure = grpc.StatusCode.UNAVAILABLE
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("service-token")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        client = SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=5)
        with pytest.raises(ServiceError, match="submission outcome unknown") as caught:
            await client.submit_input("session", "same-command", "original", "original-uid", "original input")
    assert caught.value.code == grpc.StatusCode.UNAVAILABLE
    assert len(backend.requests) == 1


if __name__ == "__main__":
    pytest_bazel.main()
