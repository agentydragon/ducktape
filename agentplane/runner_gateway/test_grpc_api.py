"""Real gRPC and TokenReview, with a scripted runner on the Sandbox's control stream for the edges a
real runner cannot be made to show."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import MessageToDict, ParseDict
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.proto import command_pb2, runner_control_pb2, runner_gateway_pb2, runner_pb2, sandbox_service_pb2
from agentplane.proto.sandbox_service_pb2 import ResolvedGrant, SandboxDestination
from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.commands import db
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import CommandSubmission, SubmissionState, SubmissionStore
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.control_listener import wait_connected
from agentplane.runner_gateway.testing.grpc_service import service_client
from agentplane.runner_gateway.testing.journal import admission
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import REPLICA, RUNNER_TOKEN, serve_replica_control
from agentplane.runner_gateway.testing.sessions import register_session
from agentplane.sandbox_service import wire
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    PROVISIONING_ANNOTATION,
)
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, DESTINATION, OWNER, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-sandbox-service"
SPEC: dict[str, object] = {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/state"}

type Connect = grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame]


@dataclass
class Peer:
    """A runner holding the Sandbox's control stream. It answers session listings from `sessions` and
    queues every other lifecycle request and every command delivery for the test to answer."""

    call: Connect
    sessions: dict[str, runner_pb2.SessionSummary] = field(default_factory=dict)
    requests: asyncio.Queue[runner_control_pb2.ControlRequest] = field(default_factory=asyncio.Queue)
    deliveries: asyncio.Queue[runner_control_pb2.DeliverCommand] = field(default_factory=asyncio.Queue)
    # Every request and delivery the service sent, listings included.
    received: list[runner_control_pb2.ServiceFrame] = field(default_factory=list)
    _writing: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def write(self, frame: runner_control_pb2.RunnerFrame) -> None:
        async with self._writing:
            await self.call.write(frame)

    async def serve(self) -> None:
        while (frame := await self.call.read()) is not grpc.aio.EOF:
            assert isinstance(frame, runner_control_pb2.ServiceFrame)
            match frame.WhichOneof("frame"):
                case "heartbeat":
                    await self.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
                case "control" if frame.control.HasField("list_sessions"):
                    self.received.append(frame)
                    result = runner_control_pb2.ControlResult(request_id=frame.control.request_id)
                    result.list_sessions.sessions.extend(self.sessions.values())
                    await self.answer(result)
                case "control":
                    self.received.append(frame)
                    self.requests.put_nowait(frame.control)
                case "deliver":
                    self.received.append(frame)
                    self.deliveries.put_nowait(frame.deliver)

    async def request(self) -> runner_control_pb2.ControlRequest:
        async with asyncio.timeout(10):
            return await self.requests.get()

    async def answer(self, result: runner_control_pb2.ControlResult) -> None:
        await self.write(runner_control_pb2.RunnerFrame(control_result=result))

    async def open(self) -> runner_control_pb2.OpenSession:
        """Answer the next request, an open, with its Session running."""
        request = await self.request()
        assert request.WhichOneof("verb") == "open"
        await self.answer(
            runner_control_pb2.ControlResult(
                request_id=request.request_id, open=attached(request.open.session_id, runner_pb2.HARNESS_STATE_RUNNING)
            )
        )
        return request.open

    async def delivery(self) -> runner_control_pb2.DeliverCommand:
        async with asyncio.timeout(10):
            return await self.deliveries.get()

    async def admit(self, delivered: runner_control_pb2.DeliverCommand, cursor: int) -> None:
        await self.write(
            runner_control_pb2.RunnerFrame(
                receipt=runner_control_pb2.CommandReceipt(
                    session_id=delivered.session_id,
                    command_id=delivered.command.command_id,
                    admitted=admission(delivered.command, cursor),
                )
            )
        )


def attached(session_id: str, harness_state: runner_pb2.HarnessState) -> runner_pb2.Attached:
    return runner_pb2.Attached(
        session_id=session_id, harness_state=harness_state, setup_state=runner_pb2.SETUP_STATE_NOT_REQUIRED
    )


async def wait_attempt_lapsed(engine: AsyncEngine, session_id: UUID, command_id: str) -> None:
    """Until the submission's dispatch attempt is past its deadline. A retry inside the open attempt
    only extends it; the gateway's request timeout can end the caller's call before that deadline."""
    submission = db.CommandSubmission
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(5), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            async with engine.connect() as connection:
                assert await connection.scalar(
                    select(submission.attempt_deadline <= func.now()).where(
                        submission.session_id == session_id, submission.command_id == command_id
                    )
                )


async def register(engine: AsyncEngine, session_id: UUID) -> None:
    """Register a Session on the fake cluster's Sandbox."""
    await register_session(
        engine, session_id, sandbox_namespace=SANDBOX_NAMESPACE, sandbox_name=SANDBOX, sandbox_uid=UUID(SANDBOX_UID)
    )


@pytest.fixture
def resources(cluster: Cluster, engine: AsyncEngine, leases: ConnectionLeases) -> Resources:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-caller",
        pod_uid="test-caller-uid",
        audiences=(AUDIENCE,),
    )
    return replace(
        build_resources(
            engine,
            principals=cluster.build_principal_resolver(AUDIENCE),
            destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
            caller_accounts=frozenset({OWNER}),
            platform_instructions="Test guidance",
        ),
        admission_timeout_s=1,
        delivery=CommandDelivery(
            engine.url, dispatches=DispatchStore(engine), leases=leases, liveness_s=1, recheck_s=0.1
        ),
        control=LifecycleControl(leases),
    )


@pytest.fixture
async def peer(cluster: Cluster, resources: Resources) -> AsyncIterator[Peer]:
    async with (
        resources.delivery.listener.listen(),
        serve_replica_control(
            cluster,
            REPLICA,
            delivery=resources.delivery,
            control=resources.control,
            timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1, max_age_s=60),
        ) as channel,
    ):
        call = channel.Connect(metadata=(("authorization", f"Bearer {RUNNER_TOKEN}"),), timeout=60)
        await call.write(runner_control_pb2.RunnerFrame(hello=runner_control_pb2.Hello(runner_boot_id="test-boot")))
        welcome = await call.read()
        assert isinstance(welcome, runner_control_pb2.ServiceFrame)
        assert welcome.HasField("welcome")
        peer = Peer(call)
        serving = asyncio.create_task(peer.serve())
        try:
            await wait_connected(resources, UUID(SANDBOX_UID))
            yield peer
        finally:
            call.cancel()
            serving.cancel()
            # The stream ends with the test's own cancellation, or with the service's when a test
            # takes the runner's identity away.
            with suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
                await serving


@pytest.fixture
async def remote(cluster: Cluster, resources: Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    async with service_client(resources, token_file, cluster.provisioning) as client:
        yield client


async def test_managed_open_uses_the_client_session_id_and_retries_on_the_runner(
    cluster: Cluster, resources: Resources, token_file: Path, peer: Peer
) -> None:
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        overrides: dict[str, object] = SPEC | {"cwd": "/state/{session_id}"}
        requested = uuid4()
        creating = asyncio.create_task(runner.create(session_id=requested, spec=overrides))
        first = await peer.open()
        created = await creating
        public = created.session_id
        # The runner's Session ID is the client's, as the ingress announces it.
        assert public == str(requested) == created.attached.session_id == first.session_id
        assert first.spec.cwd == f"/state/{public}"
        assert public in first.spec.instructions

        # After a lost reply the retry reaches the runner again, which confirms it against the spec
        # it recorded.
        retrying = asyncio.create_task(runner.create(session_id=requested, spec=overrides))
        assert await peer.open() == first
        assert await retrying == created

        resuming = asyncio.create_task(runner.resume(public))
        resume = await peer.request()
        assert resume.resume.session_id == public
        await peer.answer(
            runner_control_pb2.ControlResult(
                request_id=resume.request_id, resume=attached(public, runner_pb2.HARNESS_STATE_RUNNING)
            )
        )
        assert await resuming == created.attached


async def test_public_session_command_survives_a_lost_reply(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    peer: Peer,
    engine: AsyncEngine,
    submissions: SubmissionStore,
) -> None:
    session_id = uuid4()
    await register(engine, session_id)
    command = command_pb2.Command(command_id="durable", submit_input=command_pb2.SubmitInput(text="hi"))
    async with asyncio.timeout(10), service_client(resources, token_file, cluster.provisioning) as remote:
        with pytest.raises(TimeoutError):
            await remote.command(str(session_id), command)
        lost = await peer.delivery()
        assert (lost.session_id, lost.command) == (str(session_id), command)
        # Without the runner's receipt the submission stays pending.
        assert (await submissions.read(session_id, "durable")).state == SubmissionState.PENDING_ADMISSION
        await wait_attempt_lapsed(engine, session_id, "durable")

        # Once the attempt has lapsed, the caller's retry delivers it again, and the runner's receipt settles it.
        retrying = asyncio.create_task(remote.command(str(session_id), command))
        assert await peer.delivery() == lost
        await peer.admit(lost, 1)
        receipt = await retrying
        assert receipt == admission(command, 1)
        assert (await submissions.read(session_id, "durable")).state == SubmissionState.ADMITTED

        assert await remote.command(str(session_id), command) == receipt
        assert peer.deliveries.empty()  # an admitted retry needs no runner
        changed = command_pb2.Command(command_id="durable", submit_input=command_pb2.SubmitInput(text="changed"))
        with pytest.raises(ServiceError) as conflict:
            await remote.command(str(session_id), changed)
        assert conflict.value.code == grpc.StatusCode.ALREADY_EXISTS


async def test_command_status_reads_retained_submissions_without_the_runner(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    engine: AsyncEngine,
    sessions: SessionRegistry,
    submissions: SubmissionStore,
) -> None:
    session_id = uuid4()
    await register(engine, session_id)
    commands = {
        name: command_pb2.Command(command_id=name, submit_input=command_pb2.SubmitInput(text=name))
        for name in ("admitted", "pending", "rejected")
    }
    for command in commands.values():
        await submissions.accept(session_id, CommandSubmission(command=command), caller=OWNER)
    receipt = admission(commands["admitted"], 1)
    await submissions.record_admission(session_id, receipt)
    await submissions.record_rejection(session_id, "rejected", "test refusal")
    async with service_client(
        replace(resources, sessions=sessions, submissions=submissions), token_file, cluster.provisioning
    ) as remote:

        async def get(command_id: str, at: str = str(session_id)) -> sandbox_service_pb2.CommandStatus:
            return await remote.unary(
                remote.gateway.GetCommand, sandbox_service_pb2.GetCommandRequest(session_id=at, command_id=command_id)
            )

        async def page(request: sandbox_service_pb2.ListCommandsRequest) -> sandbox_service_pb2.ListCommandsResponse:
            request.session_id = str(session_id)
            return await remote.unary(remote.gateway.ListCommands, request)

        admitted = await get("admitted")
        assert (admitted.command, admitted.state, admitted.admission) == (
            commands["admitted"],
            sandbox_service_pb2.COMMAND_STATE_ADMITTED,
            receipt,
        )
        pending = await get("pending")
        assert pending.state == sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION
        assert not pending.HasField("admission")
        rejected = await get("rejected")
        assert (rejected.state, rejected.rejection) == (sandbox_service_pb2.COMMAND_STATE_REJECTED, "test refusal")

        listed: list[sandbox_service_pb2.CommandStatus] = []
        token = ""
        while True:
            response = await page(sandbox_service_pb2.ListCommandsRequest(page_token=token, page_size=2))
            listed.extend(response.commands)
            if not (token := response.next_page_token):
                break
        assert listed == [admitted, pending, rejected]
        only_pending = await page(
            sandbox_service_pb2.ListCommandsRequest(states=[sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION])
        )
        assert list(only_pending.commands) == [pending]

        for command_id, at, code in (
            ("unknown", str(session_id), grpc.StatusCode.NOT_FOUND),
            ("pending", str(uuid4()), grpc.StatusCode.NOT_FOUND),
            ("pending", "legacy-runner-session", grpc.StatusCode.INVALID_ARGUMENT),
        ):
            with pytest.raises(ServiceError) as refused:
                await get(command_id, at)
            assert refused.value.code == code


@pytest.mark.parametrize("session_id", ["", "r-not-a-uuid"])
async def test_open_requires_a_client_session_id(resources: Resources, token_file: Path, session_id: str) -> None:
    request = wire.create_proto(DESTINATION, uuid4(), {"harness": "HARNESS_CODEX", "cwd": "/state", "model": "m"}, None)
    request.session_id = session_id
    async with service_client(resources, token_file) as remote:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await remote.gateway.CreateSession(request, metadata=await remote.gateway_metadata(), timeout=5)
    assert refused.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_managed_open_expands_default_cwd_with_the_session_id(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster
) -> None:
    cluster.set_binding(
        sandbox_service_pb2.SandboxBinding(
            session_defaults=sandbox_service_pb2.SessionDefaults(
                harness=runner_pb2.HARNESS_CODEX, model="test-model", cwd="/state/workspaces/{session_id}"
            )
        )
    )
    creating = asyncio.create_task(remote.runner(DESTINATION).create(session_id=uuid4(), spec={}))
    opened = await peer.open()
    assert opened.spec.cwd == f"/state/workspaces/{(await creating).session_id}"


async def test_command_uses_runner_ack_budget_not_unary_request_budget(
    resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine
) -> None:
    command = command_pb2.Command(command_id="slow-ack", submit_input=command_pb2.SubmitInput(text="hi"))
    # Unary authorization/destination work must not cap the runner's separate receipt wait.
    async with service_client(
        replace(resources, admission_timeout_s=0.1, runner_admission_ack_timeout_s=1), token_file
    ) as remote:
        remote.request_timeout_s = 0.1  # The client also must not use its short generic RPC deadline.
        session_id = uuid4()
        await register(engine, session_id)
        submitted = asyncio.create_task(remote.command(str(session_id), command))
        delivered = await peer.delivery()
        await asyncio.sleep(0.2)
        await peer.admit(delivered, 1)
        assert await submitted == admission(command, 1)


@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("authorization", "Bearer invalid"),),
        (("authorization", f"Bearer {CALLER_TOKEN}"), ("authorization", f"Bearer {CALLER_TOKEN}")),
    ],
)
async def test_missing_invalid_or_duplicate_bearer_is_rejected(
    remote: SandboxServiceClient, metadata: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as rejected:
        await remote.gateway.ListSessions(
            sandbox_service_pb2.SandboxRequest(destination=DESTINATION), metadata=metadata
        )
    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"owner": sandbox_service_pb2.ServiceAccount(namespace=SANDBOX_NAMESPACE, name="other")},
            grpc.StatusCode.NOT_FOUND,
        ),
        ({"sandbox_uid": str(uuid4())}, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_destination_authority_and_incarnation(
    remote: SandboxServiceClient, change: dict[str, object], code: grpc.StatusCode
) -> None:
    destination = SandboxDestination()
    destination.CopyFrom(DESTINATION)
    if "owner" in change:
        destination.owner.name = "other"
    else:
        destination.sandbox_uid = str(change["sandbox_uid"])
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(destination).list_sessions()
    assert rejected.value.code == code


async def test_only_the_sandbox_service_may_seal(remote: SandboxServiceClient) -> None:
    # An allowed service caller is not the Sandbox Service: sealing a Sandbox is teardown's alone.
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).seal()
    assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.parametrize("account_name", [ACCOUNT, "unlisted-service"])
async def test_unlisted_callers_rejected_before_lookup(
    resources: Resources, token_file: Path, cluster: Cluster, account_name: str
) -> None:
    # Owning the destination does not grant service access. The same allowlist gates all RPCs.
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{account_name}",
        pod_name="test-unlisted-pod",
        pod_uid="test-unlisted-uid",
        audiences=(AUDIENCE,),
    )
    configured = replace(
        resources, caller_accounts=frozenset({ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="allowed-service")})
    )
    cluster.fake.objects[SANDBOXES_PLURAL].clear()
    destination = sandbox_service_pb2.SessionDestination(sandbox=DESTINATION, session_id="session")
    async with service_client(configured, token_file, cluster.provisioning) as remote:
        for call, request in (
            (remote.stub.ListSandboxes, Empty()),
            (remote.stub.GetSandbox, sandbox_service_pb2.GetSandboxRequest(name=SANDBOX)),
            (remote.stub.CreateSandbox, sandbox_service_pb2.CreateSandboxRequest()),
            (remote.stub.SuspendSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ResumeSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.DeleteSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ListTemplates, Empty()),
            (remote.stub.GrantEgress, sandbox_service_pb2.GrantEgressRequest(destination=DESTINATION)),
            (remote.stub.RevokeEgress, sandbox_service_pb2.RevokeEgressRequest()),
            (remote.gateway.ListSessions, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.gateway.ResumeSession, sandbox_service_pb2.SessionRequest(destination=destination)),
            (remote.gateway.SubmitCommand, sandbox_service_pb2.SubmitCommandRequest(session_id=str(uuid4()))),
        ):
            with pytest.raises(ServiceError) as rejected:
                await remote.unary(call, request)
            assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(ServiceError) as rejected:
            await remote.runner(DESTINATION).list_sessions()
        assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert cluster.fake.pod_reads == 0


@pytest.mark.parametrize("pending_launch", [False, True])
async def test_open_and_resume_refuse_unready_grants_without_app(
    resources: Resources,
    token_file: Path,
    cluster: Cluster,
    peer: Peer,
    engine: AsyncEngine,
    sessions: SessionRegistry,
    pending_launch: bool,
) -> None:
    session_id = uuid4()
    await register(engine, session_id)
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
                sandbox_service_pb2.KubernetesGrant(),
            ),
        )
        annotations[KUBERNETES_GRANTS_ANNOTATION] = json.dumps([MessageToDict(grant, preserving_proto_field_name=True)])
        annotations[KUBERNETES_GRANTS_READY_ANNOTATION] = "false"
    view = await cluster.inventory.get(SANDBOX)
    if pending_launch:
        assert view.launch_grants_pending
    else:
        assert not view.kubernetes_grants_ready
    configured = replace(
        resources, sessions=sessions, caller_accounts=frozenset({OWNER}), platform_instructions="Test guidance"
    )
    async with service_client(configured, token_file, cluster.provisioning) as caller:
        runner = caller.runner(DESTINATION)
        with pytest.raises(ServiceError) as opened:
            await runner.create(session_id=uuid4(), spec={"harness": "HARNESS_CODEX", "cwd": "/w", "model": "test"})
        assert opened.value.code == grpc.StatusCode.UNAVAILABLE
        with pytest.raises(ServiceError) as resumed:
            await runner.resume(str(session_id))
        assert resumed.value.code == grpc.StatusCode.UNAVAILABLE
    assert not peer.received


@pytest.mark.parametrize(
    "command",
    [
        command_pb2.Command(),
        command_pb2.Command(command_id="missing-operation"),
        command_pb2.Command(submit_input=command_pb2.SubmitInput(text="missing-id")),
    ],
)
async def test_invalid_command_is_not_submitted(
    remote: SandboxServiceClient, cluster: Cluster, command: command_pb2.Command
) -> None:
    with pytest.raises(ServiceError) as rejected:
        await remote.command(str(uuid4()), command)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert cluster.fake.pod_reads == 0


@pytest.mark.parametrize("field", ["controller", "uid", "name", "kind", "apiVersion"])
async def test_unverified_pod_owner_is_unavailable(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, field: str
) -> None:
    cluster.fake.pods[SANDBOX]["metadata"]["ownerReferences"][0][field] = (
        False if field == "controller" else "test-foreign"
    )
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert not peer.received


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
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert not peer.received


async def test_session_sandboxes_answer_from_the_registry_including_deleted_sandboxes(
    cluster: Cluster, resources: Resources, token_file: Path, engine: AsyncEngine
) -> None:
    live_id, deleted_id, unknown_id = uuid4(), uuid4(), uuid4()
    await register(engine, live_id)
    await register_session(
        engine, deleted_id, sandbox_namespace=SANDBOX_NAMESPACE, sandbox_name="Deleted-Sandbox", sandbox_uid=None
    )
    live = runner_gateway_pb2.SessionSandbox(
        session_id=str(live_id), sandbox_namespace=SANDBOX_NAMESPACE, sandbox=SANDBOX, sandbox_uid=SANDBOX_UID
    )
    deleted = runner_gateway_pb2.SessionSandbox(
        session_id=str(deleted_id), sandbox_namespace=SANDBOX_NAMESPACE, sandbox="Deleted-Sandbox"
    )
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        got = await remote.session_sandboxes({str(live_id), str(deleted_id), str(unknown_id)})
        assert sorted(got, key=lambda found: found.session_id) == sorted(
            [live, deleted], key=lambda found: found.session_id
        )
        assert await remote.find_sessions(runner_gateway_pb2.FindSessionsRequest(sandbox_name=SANDBOX)) == [live]
        # Case-insensitive, and a LIKE wildcard in the text is matched literally.
        contains = runner_gateway_pb2.FindSessionsRequest(sandbox_name_contains="deleted-")
        assert await remote.find_sessions(contains) == [deleted]
        wildcard = runner_gateway_pb2.FindSessionsRequest(sandbox_name_contains="%")
        assert await remote.find_sessions(wildcard) == []
        with pytest.raises(ServiceError) as unmatched:
            await remote.find_sessions(runner_gateway_pb2.FindSessionsRequest())
        assert unmatched.value.code == grpc.StatusCode.INVALID_ARGUMENT


if __name__ == "__main__":
    pytest_bazel.main()
