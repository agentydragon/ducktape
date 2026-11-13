"""Native Dispatcher transport works without an integration app or browser session."""

from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError
from agentplane.grpc.serving import serving
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.testing.tls import issue_leaf, make_ca

# The generated Dispatcher stubs' own pyi chain imports google.protobuf.
# gazelle:include_dep @pypi//protobuf


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
        self.bearers.append(dict(context.invocation_metadata() or ()).get("authorization"))
        if self.failure is not None:
            await context.abort(self.failure, "submission outcome unknown")
        return session_dispatcher_pb2.SubmitInputResponse(
            dispatch=session_dispatcher_pb2.Dispatch(
                command_id=request.command_id,
                session_id=request.session_id,
                sandbox="original",
                sandbox_uid=request.expected_sandbox_uid,
                state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
            )
        )

    @override
    async def GetSession(
        self,
        request: session_dispatcher_pb2.GetSessionRequest,
        context: grpc.aio.ServicerContext[
            session_dispatcher_pb2.GetSessionRequest, session_dispatcher_pb2.GetSessionResponse
        ],
    ) -> session_dispatcher_pb2.GetSessionResponse:
        self.bearers.append(dict(context.invocation_metadata() or ()).get("authorization"))
        return session_dispatcher_pb2.GetSessionResponse(
            session=session_dispatcher_pb2.Session(
                session_id=request.session_id,
                status=session_dispatcher_pb2.SessionStatus(
                    materialization=session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
                ),
                placement=session_pb2.SessionPlacement(
                    environment_hosted=session_pb2.EnvironmentHostedPlacement(
                        environment=session_pb2.ExactEnvironment(name="original", uid="original-uid")
                    )
                ),
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
        session = await client.get_session("session")
        assert (
            session.session_id,
            session.placement.environment_hosted.environment.name,
            session.placement.environment_hosted.environment.uid,
        ) == ("session", "original", "original-uid")
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
                expected_sandbox_uid="original-uid",
                text="original input",
            )
        ]
        * 2
    )
    assert backend.bearers == [
        "Bearer first-service-token",
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
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SessionDispatcherClient.connect(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=5) as client,
    ):
        with pytest.raises(ServiceError, match="submission outcome unknown") as caught:
            await client.submit_input("session", "same-command", "original", "original-uid", "original input")
    assert caught.value.code == grpc.StatusCode.UNAVAILABLE
    assert len(backend.requests) == 1


async def test_native_client_verifies_tls_through_a_port_forward(tmp_path: Path) -> None:
    ca = make_ca("test dispatcher CA")
    certificate, key = issue_leaf(ca, "localhost", tmp_path)
    backend = Dispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(backend, server)
    port = server.add_secure_port(
        "127.0.0.1:0", grpc.ssl_server_credentials([(key.read_bytes(), certificate.read_bytes())])
    )
    token_file = tmp_path / "dispatcher-token"
    token_file.write_text("test-service-token")
    await server.start()
    try:
        async with SessionDispatcherClient.connect(
            f"127.0.0.1:{port}",
            token_file=token_file,
            request_timeout_s=3,
            channel_credentials=grpc.ssl_channel_credentials(root_certificates=ca.cert_pem),
            server_name="localhost",
        ) as client:
            session = await client.get_session("test-session")
        assert session.session_id == "test-session"
        assert backend.bearers == ["Bearer test-service-token"]
    finally:
        await server.stop(grace=0)


if __name__ == "__main__":
    pytest_bazel.main()
