"""Real gRPC and TokenReview, with a controllable runner peer for transport failure edges."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import MessageToDict, ParseDict
from kubernetes_asyncio import client as k8s_client

from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service import protocol_pb2, wire
from agentplane.sandbox_service.client import ReconnectRequiredError, SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    PROVISIONING_ANNOTATION,
)
from agentplane.sandbox_service.protocol_pb2 import ResolvedGrant, SandboxDestination
from agentplane.sandbox_service.testing.grpc_service import service_client
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

TOKEN = "test-grpc-token"
AUDIENCE = "test-sandbox-service"
OWNER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
DESTINATION = SandboxDestination(
    owner=protocol_pb2.ServiceAccount(namespace=OWNER.namespace, name=OWNER.name),
    sandbox=SANDBOX,
    sandbox_uid=SANDBOX_UID,
)


@dataclass
class PeerAttachment:
    opened: runner_pb2.Open
    commands: asyncio.Queue[runner_pb2.ClientMessage] = field(default_factory=asyncio.Queue)
    responses: asyncio.Queue[runner_pb2.ServerMessage | grpc.StatusCode | None] = field(default_factory=asyncio.Queue)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class Peer:
    def __init__(self) -> None:
        self.attachments: asyncio.Queue[PeerAttachment] = asyncio.Queue()
        self.port = 0
        self.state = runner_pb2.HARNESS_STATE_RUNNING
        self.answer_open = True

    async def attach(
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        first = await anext(requests)
        connection = PeerAttachment(first.open)
        self.attachments.put_nowait(connection)

        async def consume() -> None:
            async for request in requests:
                connection.commands.put_nowait(request)

        consumer = asyncio.create_task(consume())
        try:
            if self.answer_open:
                yield runner_pb2.ServerMessage(
                    attached=runner_pb2.Attached(session_id=first.open.session_id, harness_state=self.state)
                )
            while (message := await connection.responses.get()) is not None:
                if isinstance(message, grpc.StatusCode):
                    await context.abort(message, "test runner failure")
                else:
                    yield message
        finally:
            consumer.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await consumer
            finally:
                # Aborting the server RPC can also fail its request-reader task. Still record
                # completed cleanup, rather than making the test wait on an unreachable marker.
                connection.closed.set()


@pytest.fixture
async def peer() -> AsyncIterator[Peer]:
    peer = Peer()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                "ducktape.agentplane.runner.v1.Runner",
                {
                    "Attach": grpc.stream_stream_rpc_method_handler(
                        peer.attach,
                        request_deserializer=runner_pb2.ClientMessage.FromString,
                        response_serializer=runner_pb2.ServerMessage.SerializeToString,
                    )
                },
            )
        ]
    )
    peer.port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield peer
    finally:
        await server.stop(0)


@pytest.fixture
def resources(cluster: Cluster, peer: Peer) -> Resources:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-caller",
        pod_uid="test-caller-uid",
        audiences=(AUDIENCE,),
    )
    return Resources(
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=AUDIENCE,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        provisioning=cluster.provisioning,
        destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), peer.port),
        caller_accounts=frozenset({OWNER}),
        platform_instructions="Test guidance",
        follow_lease_s=0.5,
        admission_timeout_s=1,
    )


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN)
    return path


@pytest.fixture
async def remote(resources: Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    async with service_client(resources, token_file) as client:
        yield client


def admission(command: command_pb2.Command, cursor: int) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-journal", sequence=cursor),
        event=event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command)),
    )


async def test_admission_is_exact_native_evidence(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="notice", submit_input=command_pb2.SubmitInput(text="7 messages"))
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(remote.runner(DESTINATION).command("session", command, after_cursor=4))
        connection = await peer.attachments.get()
        assert connection.opened.follow.after_cursor == 4
        assert not connection.opened.HasField("spec")
        assert not connection.opened.HasField("setup_script")
        assert (await connection.commands.get()).command == command
        assert (await connection.commands.get()).HasField("detach")
        wrong = command_pb2.Command(command_id="notice", submit_input=command_pb2.SubmitInput(text="different"))
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=admission(wrong, 5)))
        assert not pending.done()
        other = command_pb2.Command(command_id="other", submit_input=command.submit_input)
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=admission(other, 6)))
        receipt = admission(command, 7)
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=receipt))
        assert await pending == receipt
        await connection.closed.wait()


async def test_idle_follow_renews_with_ok_status_not_native_end(
    resources: Resources, token_file: Path, peer: Peer
) -> None:
    # Longer than admission/write deadlines: idle native reads must not use those deadlines.
    async with service_client(replace(resources, admission_timeout_s=0.2, follow_lease_s=0.6), token_file) as remote:
        async with asyncio.timeout(8):
            call = remote.stub.FollowSession(
                protocol_pb2.FollowSessionRequest(
                    destination=protocol_pb2.SessionDestination(sandbox=DESTINATION, session_id="session")
                ),
                metadata=await remote.metadata(),
                timeout=5,
            )
            try:
                assert (await call.read()).HasField("attached")
                connection = await peer.attachments.get()
                assert (await call.read()).HasField("reconnect_required")
                assert await call.read() is grpc.aio.EOF
                assert await call.code() == grpc.StatusCode.OK
                await connection.closed.wait()
            finally:
                call.cancel()


async def test_stalled_downstream_write_expires_before_follow_renewal(
    resources: Resources, token_file: Path, peer: Peer
) -> None:
    async with service_client(replace(resources, admission_timeout_s=1, follow_lease_s=30), token_file) as remote:
        async with asyncio.timeout(8):
            attachment = await remote.runner(DESTINATION).attach("session")
            connection = await peer.attachments.get()
            try:
                # Exceed the gRPC receive window without reading from the downstream stream.
                message = runner_pb2.ServerMessage(
                    event_entry=event_log_pb2.EventEntry(
                        cursor=1, event=event_pb2.Event(native=event_pb2.Native(line="x" * 1024 * 1024))
                    )
                )
                for _ in range(64):
                    connection.responses.put_nowait(message)
                await connection.closed.wait()

                # A failed write is a deadline, not a successful planned renewal or native EOF.
                async def drain() -> None:
                    while True:
                        await attachment.next_entry()

                with pytest.raises(TimeoutError, match="follow deadline"):
                    await drain()
            finally:
                attachment.cancel()


async def test_follow_safety_deadline_is_not_planned_renewal(remote: SandboxServiceClient, peer: Peer) -> None:
    remote.follow_timeout_s = 0.2
    async with asyncio.timeout(8):
        attachment = await remote.runner(DESTINATION).attach("session")
        connection = await peer.attachments.get()
        with pytest.raises(TimeoutError, match="follow deadline"):
            await attachment.next_entry()
        await connection.closed.wait()


async def test_client_initial_attachment_keeps_short_deadline(remote: SandboxServiceClient, peer: Peer) -> None:
    remote.request_timeout_s = 0.2
    peer.answer_open = False
    async with asyncio.timeout(8):
        with pytest.raises(TimeoutError):
            await remote.runner(DESTINATION).attach("session")
        connection = await peer.attachments.get()
        await connection.closed.wait()


async def test_follow_reconnect_rechecks_token_and_preserves_cursor(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster
) -> None:
    async with asyncio.timeout(8):
        runner = remote.runner(DESTINATION)
        attachment = await runner.attach("session", after_cursor=11)
        connection = await peer.attachments.get()
        assert connection.opened.follow.after_cursor == 11
        assert attachment.attached.session_id == "session"
        receipt = admission(command_pb2.Command(command_id="recorded"), 12)
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=receipt))
        assert await attachment.next_entry() == receipt
        with pytest.raises(ReconnectRequiredError):
            await attachment.next_entry()
        await connection.closed.wait()
        second = await runner.attach("session", after_cursor=12)
        next_connection = await peer.attachments.get()
        assert next_connection.opened.follow.after_cursor == 12
        second.cancel()
        await next_connection.closed.wait()
        # Rotation is read per request; a stale or revoked credential cannot reuse the channel's identity.
        remote.token_file.write_text("invalid-token")
        with pytest.raises(ServiceError) as rejected:
            await runner.attach("session", after_cursor=12)
        assert rejected.value.code == grpc.StatusCode.UNAUTHENTICATED
        assert peer.attachments.empty()
        remote.token_file.write_text(TOKEN)
        cluster.fake.tokens.clear()
        with pytest.raises(ServiceError) as revoked:
            await runner.attach("session", after_cursor=12)
        assert revoked.value.code == grpc.StatusCode.UNAUTHENTICATED
        assert peer.attachments.empty()


@pytest.mark.parametrize("ending", [None, grpc.StatusCode.UNAVAILABLE])
async def test_native_eof_is_distinct_from_backend_failure(
    remote: SandboxServiceClient, peer: Peer, ending: grpc.StatusCode | None
) -> None:
    async with asyncio.timeout(8):
        attachment = await remote.runner(DESTINATION).attach("session")
        connection = await peer.attachments.get()
        connection.responses.put_nowait(ending)
        if ending is None:
            with pytest.raises(StreamClosedError):
                await attachment.next_entry()
        else:
            with pytest.raises(ServiceError) as unavailable:
                await attachment.next_entry()
            assert unavailable.value.code == grpc.StatusCode.UNAVAILABLE
        await connection.closed.wait()


async def test_cancellation_closes_runner_attachment(remote: SandboxServiceClient, peer: Peer) -> None:
    async with asyncio.timeout(8):
        attachment = await remote.runner(DESTINATION).attach("session")
        connection = await peer.attachments.get()
        attachment.cancel()
        await connection.closed.wait()


async def test_unanswered_runner_open_is_a_deadline_not_a_state_rejection(
    remote: SandboxServiceClient, peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer.answer_open = False
    monkeypatch.setattr("agentplane.runner.client.OBSERVE_ANSWER_S", 0.05)
    async with asyncio.timeout(8):
        with pytest.raises(TimeoutError, match="uncertain"):
            await remote.runner(DESTINATION).attach("session")
        connection = await peer.attachments.get()
        await connection.closed.wait()
        assert connection.commands.empty()


async def test_timeout_is_uncertain_and_closes_attachment(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="uncertain", submit_input=command_pb2.SubmitInput(text="hi"))

    async def submit() -> None:
        with pytest.raises(TimeoutError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)

    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(submit())
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        await pending
        await connection.closed.wait()
        assert peer.attachments.empty()  # The client did not retry the uncertain mutation.


async def test_stopped_command_does_not_send_input(remote: SandboxServiceClient, peer: Peer) -> None:
    peer.state = runner_pb2.HARNESS_STATE_STOPPED
    command = command_pb2.Command(command_id="stopped", submit_input=command_pb2.SubmitInput(text="hi"))
    async with asyncio.timeout(8):
        with pytest.raises(RunnerError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)
        connection = await peer.attachments.get()
        await connection.closed.wait()
        assert not connection.opened.HasField("spec")
        assert connection.commands.empty()


@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("authorization", "Bearer invalid"),),
        (("authorization", f"Bearer {TOKEN}"), ("authorization", f"Bearer {TOKEN}")),
    ],
)
async def test_missing_invalid_or_duplicate_bearer_is_rejected(
    remote: SandboxServiceClient, peer: Peer, metadata: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as rejected:
        await remote.stub.ListSessions(protocol_pb2.SandboxRequest(destination=DESTINATION), metadata=metadata)
    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert peer.attachments.empty()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"owner": protocol_pb2.ServiceAccount(namespace=SANDBOX_NAMESPACE, name="other")}, grpc.StatusCode.NOT_FOUND),
        ({"sandbox_uid": str(uuid4())}, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_destination_authority_and_incarnation(
    remote: SandboxServiceClient, peer: Peer, change: dict[str, object], code: grpc.StatusCode
) -> None:
    destination = SandboxDestination()
    destination.CopyFrom(DESTINATION)
    if "owner" in change:
        destination.owner.name = "other"
    else:
        destination.sandbox_uid = str(change["sandbox_uid"])
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(destination).attach("session")
    assert rejected.value.code == code
    assert peer.attachments.empty()


@pytest.mark.parametrize("account_name", [ACCOUNT, "unlisted-service"])
async def test_unlisted_callers_rejected_before_lookup(
    resources: Resources, token_file: Path, peer: Peer, cluster: Cluster, account_name: str
) -> None:
    # Owning the destination does not grant service access. The same allowlist gates all RPCs.
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{account_name}",
        pod_name="test-unlisted-pod",
        pod_uid="test-unlisted-uid",
        audiences=(AUDIENCE,),
    )
    configured = replace(
        resources, caller_accounts=frozenset({ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="allowed-service")})
    )
    cluster.fake.objects[SANDBOXES_PLURAL].clear()
    destination = protocol_pb2.SessionDestination(sandbox=DESTINATION, session_id="session")
    async with service_client(configured, token_file) as remote:
        for call, request in (
            (remote.stub.ListSandboxes, Empty()),
            (remote.stub.GetSandbox, protocol_pb2.GetSandboxRequest(name=SANDBOX)),
            (remote.stub.CreateSandbox, protocol_pb2.CreateSandboxRequest()),
            (remote.stub.SuspendSandbox, protocol_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ResumeSandbox, protocol_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.DeleteSandbox, protocol_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ListTemplates, Empty()),
            (remote.stub.GrantEgress, protocol_pb2.GrantEgressRequest(destination=DESTINATION)),
            (remote.stub.RevokeEgress, protocol_pb2.RevokeEgressRequest()),
            (remote.stub.ListSessions, protocol_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.OpenSession, protocol_pb2.OpenSessionRequest(destination=destination)),
            (remote.stub.ResumeSession, protocol_pb2.SessionRequest(destination=destination)),
            (remote.stub.SubmitCommand, protocol_pb2.SubmitCommandRequest(destination=destination)),
        ):
            with pytest.raises(ServiceError) as rejected:
                await remote.unary(call, request)
            assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(ServiceError) as rejected:
            await remote.runner(DESTINATION).attach("session")
        assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert cluster.fake.pod_reads == 0
    assert peer.attachments.empty()


@pytest.mark.parametrize("pending_launch", [False, True])
async def test_open_and_resume_refuse_unready_grants_without_app(
    resources: Resources, token_file: Path, cluster: Cluster, peer: Peer, pending_launch: bool
) -> None:
    annotations = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"].setdefault("annotations", {})
    if pending_launch:
        annotations[PROVISIONING_ANNOTATION] = "{}"
    else:
        grant = ResolvedGrant(
            name="config",
            grant=ParseDict(
                RoleBindingGrant(
                    kind="RoleBinding", namespace=SANDBOX_NAMESPACE, role_ref=RoleRef(kind="Role", name="config-reader")
                ).model_dump(mode="json"),
                protocol_pb2.KubernetesGrant(),
            ),
        )
        annotations[KUBERNETES_GRANTS_ANNOTATION] = json.dumps([MessageToDict(grant, preserving_proto_field_name=True)])
        annotations[KUBERNETES_GRANTS_READY_ANNOTATION] = "false"
    view = await cluster.inventory.get(SANDBOX)
    if pending_launch:
        assert view.launch_grants_pending
    else:
        assert not view.kubernetes_grants_ready
    configured = replace(resources, caller_accounts=frozenset({OWNER}), platform_instructions="Test guidance")
    async with service_client(configured, token_file) as caller:
        runner = caller.runner(DESTINATION)
        with pytest.raises(ServiceError) as opened:
            await runner.open("session", {"harness": "HARNESS_CODEX", "cwd": "/w", "model": "test"})
        assert opened.value.code == grpc.StatusCode.UNAVAILABLE
        with pytest.raises(ServiceError) as resumed:
            await runner.resume("session")
        assert resumed.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


@pytest.mark.parametrize(
    "command",
    [
        command_pb2.Command(),
        command_pb2.Command(command_id="missing-operation"),
        command_pb2.Command(submit_input=command_pb2.SubmitInput(text="missing-id")),
    ],
)
async def test_invalid_command_is_not_submitted(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, command: command_pb2.Command
) -> None:
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).command("session", command, after_cursor=0)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert cluster.fake.pod_reads == 0
    assert peer.attachments.empty()


def test_wire_preserves_explicit_empty_overrides() -> None:
    request = wire.open_proto(
        protocol_pb2.SessionDestination(sandbox=DESTINATION, session_id="session"),
        {"model": "test-model", "instructions": "", "reasoningEffort": ""},
        "",
    )
    assert wire.launch_overrides(request) == {"model": "test-model", "instructions": "", "reasoningEffort": ""}
    assert request.HasField("setup_script")
    assert request.setup_script == ""
    request.override_mask.paths.append("unknown")
    with pytest.raises(ValueError, match="unique SessionSpec field names"):
        wire.launch_overrides(request)
    request.override_mask.paths[:] = ["model"]
    request.spec.cwd = "/unselected"
    with pytest.raises(ValueError, match="every supplied SessionSpec field"):
        wire.launch_overrides(request)


async def test_bare_service_eof_is_not_native_closure(tmp_path: Path) -> None:
    async def truncated(
        request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        yield protocol_pb2.FollowSessionResponse(
            attached=runner_pb2.Attached(session_id=request.destination.session_id)
        )
        # Deliberately no ended observation: even an OK transport status is not native EOF evidence.

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                "ducktape.agentplane.sandbox.v1.SandboxService",
                {
                    "FollowSession": grpc.unary_stream_rpc_method_handler(
                        truncated,
                        request_deserializer=protocol_pb2.FollowSessionRequest.FromString,
                        response_serializer=protocol_pb2.FollowSessionResponse.SerializeToString,
                    )
                },
            )
        ]
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    client = SandboxServiceClient(f"127.0.0.1:{port}", namespace=SANDBOX_NAMESPACE, token_file=token_file)
    try:
        attachment = await client.runner(DESTINATION).attach("session")
        try:
            with pytest.raises(ConnectionError, match="without native closure"):
                await attachment.next_entry()
        finally:
            attachment.cancel()
    finally:
        await client.close()
        await server.stop(0)


def test_duplicate_spec_aliases_are_refused() -> None:
    with pytest.raises(ValueError, match="both proto and JSON"):
        wire.open_proto(
            protocol_pb2.SessionDestination(sandbox=DESTINATION, session_id="session"),
            {"reasoning_effort": "low", "reasoningEffort": "high"},
            None,
        )


async def test_command_cancellation_closes_native_attachment(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="cancel", submit_input=command_pb2.SubmitInput(text="notice"))
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(remote.runner(DESTINATION).command("session", command, after_cursor=0))
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        assert (await connection.commands.get()).HasField("detach")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await connection.closed.wait()


@pytest.mark.parametrize("error", [None, "test journal failure"])
async def test_native_eof_or_error_is_not_admission(
    remote: SandboxServiceClient, peer: Peer, error: str | None
) -> None:
    command = command_pb2.Command(command_id="end", submit_input=command_pb2.SubmitInput(text="notice"))

    async def submit() -> None:
        with pytest.raises(RunnerError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)

    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(submit())
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        connection.responses.put_nowait(runner_pb2.ServerMessage(error=error) if error else None)
        await pending
        await connection.closed.wait()
        assert peer.attachments.empty()


@pytest.mark.parametrize("field", ["controller", "uid", "name", "kind", "apiVersion"])
async def test_unverified_pod_owner_is_unavailable(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, field: str
) -> None:
    cluster.fake.pods[SANDBOX]["metadata"]["ownerReferences"][0][field] = (
        False if field == "controller" else "test-foreign"
    )
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).attach("session")
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


@pytest.mark.parametrize("condition", ["different-account", "suspended", "deleting"])
async def test_unavailable_destination_does_not_contact_runner(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, condition: str
) -> None:
    stored = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    match condition:
        case "different-account":
            cluster.fake.pods[SANDBOX]["spec"]["serviceAccountName"] = "test-other"
        case "suspended":
            stored["spec"]["operatingMode"] = "Suspended"
        case "deleting":
            stored["metadata"]["deletionTimestamp"] = "2026-09-01T12:00:00Z"
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).attach("session")
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


async def test_successor_pod_same_sandbox_and_account_keeps_destination(resources: Resources, cluster: Cluster) -> None:
    first = await resources.destinations.resolve(DESTINATION)
    assert first.target == f"127.0.0.1:{resources.destinations.runner_port}"
    cluster.fake.pods[SANDBOX]["metadata"]["uid"] = "test-successor-pod"
    cluster.fake.pods[SANDBOX]["status"]["podIP"] = "::1"
    resolved = await resources.destinations.resolve(DESTINATION)
    assert resolved.target == f"[::1]:{resources.destinations.runner_port}"


if __name__ == "__main__":
    pytest_bazel.main()
