"""Native Dispatcher transport works without an integration app or browser session."""

from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError
from agentplane.grpc.serving import serving
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient

# The generated Dispatcher stubs' own pyi chain imports google.protobuf.
# gazelle:include_dep @pypi//protobuf


class Dispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.requests: list[session_dispatcher_pb2.SubmitInputRequest] = []
        self.open_requests: list[session_dispatcher_pb2.OpenSessionRequest] = []
        self.opening_reads: list[session_dispatcher_pb2.GetSessionOpeningRequest] = []
        self.bearers: list[str | bytes | None] = []
        self.failure: grpc.StatusCode | None = None
        self.open_failure: grpc.StatusCode | None = None
        self.opening_state = session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED

    @override
    async def SubmitInput(
        self,
        request: session_dispatcher_pb2.SubmitInputRequest,
        context: grpc.aio.ServicerContext[
            session_dispatcher_pb2.SubmitInputRequest, session_dispatcher_pb2.SubmitInputResponse
        ],
    ) -> session_dispatcher_pb2.SubmitInputResponse:
        self.requests.append(request)
        self.bearers.append(dict(context.invocation_metadata() or ()).get("authorization"))
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
        self.opening_reads.append(request)
        self.bearers.append(dict(context.invocation_metadata() or ()).get("authorization"))
        return session_dispatcher_pb2.GetSessionOpeningResponse(
            opening=session_dispatcher_pb2.SessionOpening(
                session_id=request.session_id, sandbox="original", sandbox_uid="original-uid", state=self.opening_state
            )
        )

    @override
    async def OpenSession(
        self,
        request: session_dispatcher_pb2.OpenSessionRequest,
        context: grpc.aio.ServicerContext[
            session_dispatcher_pb2.OpenSessionRequest, session_dispatcher_pb2.OpenSessionResponse
        ],
    ) -> session_dispatcher_pb2.OpenSessionResponse:
        self.open_requests.append(request)
        self.bearers.append(dict(context.invocation_metadata() or ()).get("authorization"))
        if self.open_failure is not None:
            await context.abort(self.open_failure, "opening outcome unknown")
        return session_dispatcher_pb2.OpenSessionResponse(
            opening=session_dispatcher_pb2.SessionOpening(
                session_id=request.session_id,
                sandbox=request.sandbox,
                sandbox_uid=request.sandbox_uid,
                state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
            )
        )


async def test_native_submission_preserves_identity_and_rotates_service_credentials(tmp_path: Path) -> None:
    backend = Dispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("first-service-token")
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SessionDispatcherClient.connect(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=5) as client,
    ):
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


async def test_native_opening_and_reconciliation_preserve_session_and_sandbox_identity(tmp_path: Path) -> None:
    backend = Dispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("service-token")
    spec = session_pb2.SessionSpec(
        harness=session_pb2.HARNESS_CODEX,
        model="test-model",
        cwd="/state/work",
        reasoning_effort="low",
        instructions="test instructions",
    )
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SessionDispatcherClient.connect(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=5) as client,
    ):
        opening = await client.open_session("stable-session-id", "original", "original-uid", spec, setup_script="setup")
        assert opening.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        admitted = await client.wait_for_opening(opening, timeout_s=5)

    assert admitted.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
    assert backend.open_requests == [
        session_dispatcher_pb2.OpenSessionRequest(
            session_id="stable-session-id",
            sandbox="original",
            sandbox_uid="original-uid",
            spec=spec,
            setup_script="setup",
        )
    ]
    assert backend.opening_reads == [session_dispatcher_pb2.GetSessionOpeningRequest(session_id="stable-session-id")]
    assert backend.bearers == ["Bearer service-token", "Bearer service-token"]


async def test_native_opening_does_not_replay_an_unknown_write(tmp_path: Path) -> None:
    backend = Dispatcher()
    backend.open_failure = grpc.StatusCode.UNAVAILABLE
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("service-token")
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SessionDispatcherClient.connect(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=5) as client,
    ):
        with pytest.raises(ServiceError, match="opening outcome unknown") as caught:
            await client.open_session("same-session", "original", "original-uid", session_pb2.SessionSpec())
    assert caught.value.code == grpc.StatusCode.UNAVAILABLE
    assert len(backend.open_requests) == 1
    assert backend.opening_reads == []


async def test_native_submission_does_not_automatically_retry_an_unknown_outcome(tmp_path: Path) -> None:
    backend = Dispatcher()
    backend.failure = grpc.StatusCode.UNAVAILABLE
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("service-token")
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SessionDispatcherClient.connect(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=5) as client,
    ):
        with pytest.raises(ServiceError, match="submission outcome unknown") as caught:
            await client.submit_input("session", "same-command", "original", "original-uid", "original input")
    assert caught.value.code == grpc.StatusCode.UNAVAILABLE
    assert len(backend.requests) == 1


if __name__ == "__main__":
    pytest_bazel.main()
