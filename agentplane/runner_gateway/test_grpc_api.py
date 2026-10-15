"""Real gRPC and TokenReview, with a scripted runner on the Sandbox's control stream for the edges a
real runner cannot be made to show."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.grpc_calls import ServiceError, dial
from agentplane.proto import command_pb2, runner_control_pb2, runner_gateway_pb2, runner_pb2, sandboxes_pb2
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner_gateway import wire
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.auth import CALLER_TOKEN
from agentplane.runner_gateway.testing.grpc_service import gateway_client, service, service_client
from agentplane.runner_gateway.testing.journal import admission
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import (
    REPLICA,
    ScriptedRunner,
    attached,
    connected,
    serve_replica_control,
)
from agentplane.runner_gateway.testing.sessions import register_on_fake_sandbox, register_session
from agentplane.sandbox_resource import QUIESCED, SANDBOXES_PLURAL
from agentplane.sandboxes.testing.kubernetes import ACCOUNT, DESTINATION, OWNER, SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-runner-gateway"
SPEC: dict[str, object] = {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/state"}


@pytest.fixture
def resources(cluster: SandboxCluster, engine: AsyncEngine, leases: ConnectionLeases) -> Resources:
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
        ),
        admission_timeout_s=1,
        control=LifecycleControl(leases),
    )


@pytest.fixture
async def peer(cluster: SandboxCluster, resources: Resources) -> AsyncIterator[ScriptedRunner]:
    """A runner holding the Sandbox's control stream. Its connection ends with the test, or with the
    service when a test takes the runner's identity away."""
    runner = ScriptedRunner()
    async with (
        serve_replica_control(
            cluster,
            REPLICA,
            control=resources.control,
            timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1, max_age_s=60),
        ) as listener,
        connected(listener, runner, resources.control),
    ):
        yield runner


@pytest.fixture
async def remote(cluster: SandboxCluster, resources: Resources, token_file: Path) -> AsyncIterator[RunnerGatewayClient]:
    async with service_client(resources, token_file) as client:
        yield client


async def test_open_uses_the_client_session_id_and_retries_on_the_runner(
    cluster: SandboxCluster, resources: Resources, token_file: Path, peer: ScriptedRunner
) -> None:
    async with service_client(resources, token_file) as remote:
        runner = remote.runner(DESTINATION)
        requested = uuid4()
        launch_spec: dict[str, object] = SPEC | {"cwd": f"/state/{requested}"}
        creating = asyncio.create_task(runner.create(session_id=requested, spec=launch_spec))
        first = await peer.open()
        created = await creating
        public = created.session_id
        # The runner's Session ID is the client's, as the ingress announces it.
        assert public == str(requested) == created.attached.session_id == first.session_id
        assert first.spec.cwd == f"/state/{public}"

        # After a lost reply the retry reaches the runner again, which confirms it against the spec
        # it recorded.
        retrying = asyncio.create_task(runner.create(session_id=requested, spec=launch_spec))
        assert await peer.open() == first
        assert await retrying == created

        resuming = asyncio.create_task(runner.resume(public))
        resume = await peer.next(runner_control_pb2.ResumeSessionRequest)
        assert resume.request.session_id == public
        resume.outcome.set_result(
            runner_control_pb2.ResumeSessionResponse(attached=attached(public, runner_pb2.HARNESS_STATE_RUNNING))
        )
        assert await resuming == created.attached


async def test_a_retry_after_a_lost_reply_delivers_the_command_again(
    cluster: SandboxCluster, resources: Resources, token_file: Path, peer: ScriptedRunner, engine: AsyncEngine
) -> None:
    session_id = uuid4()
    await register_on_fake_sandbox(engine, session_id)
    command = command_pb2.Command(command_id="durable", submit_input=command_pb2.SubmitInput(text="hi"))
    async with asyncio.timeout(10), service_client(resources, token_file) as remote:
        with pytest.raises(TimeoutError):
            await remote.command(DESTINATION, str(session_id), command)
        lost = (await peer.next(runner_control_pb2.DeliverCommandRequest)).request
        assert (lost.session_id, lost.command) == (str(session_id), command)
        # The gateway kept nothing: the retry reaches the runner again, which deduplicates it.
        retrying = asyncio.create_task(remote.command(DESTINATION, str(session_id), command))
        retried = await peer.next(runner_control_pb2.DeliverCommandRequest)
        assert retried.request == lost
        retried.outcome.set_result(runner_control_pb2.DeliverCommandResponse(admission=admission(command, 1)))
        assert await retrying == admission(command, 1)


@pytest.mark.parametrize("session_id", ["", "r-not-a-uuid"])
async def test_open_requires_a_client_session_id(resources: Resources, token_file: Path, session_id: str) -> None:
    request = wire.create_proto(DESTINATION, uuid4(), {"harness": "HARNESS_CODEX", "cwd": "/state", "model": "m"}, None)
    request.session_id = session_id
    async with service_client(resources, token_file) as remote:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await remote.stub.CreateSession(request, metadata=await remote.metadata(), timeout=5)
    assert refused.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_complete_spec_opens_as_sent(remote: RunnerGatewayClient, peer: ScriptedRunner) -> None:
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model", cwd="/state/complete")
    request = runner_gateway_pb2.CreateSessionRequest(sandbox=DESTINATION, spec=spec, session_id=str(uuid4()))
    metadata = await remote.metadata()

    async def create() -> None:
        await remote.stub.CreateSession(request, metadata=metadata, timeout=5)

    creating = asyncio.create_task(create())
    opened = await peer.open()
    assert opened.spec == spec
    assert not opened.HasField("setup_script")
    await creating


async def test_complete_spec_still_needs_harness_cwd_and_model(remote: RunnerGatewayClient) -> None:
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, cwd="/state/complete")
    request = runner_gateway_pb2.CreateSessionRequest(sandbox=DESTINATION, spec=spec, session_id=str(uuid4()))
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await remote.stub.CreateSession(request, metadata=await remote.metadata(), timeout=5)
    assert refused.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_command_uses_runner_ack_budget_not_unary_request_budget(
    resources: Resources, token_file: Path, peer: ScriptedRunner, engine: AsyncEngine
) -> None:
    command = command_pb2.Command(command_id="slow-ack", submit_input=command_pb2.SubmitInput(text="hi"))
    # Unary authorization/destination work must not cap the runner's separate receipt wait.
    async with service_client(
        replace(resources, admission_timeout_s=0.1, runner_admission_ack_timeout_s=1), token_file
    ) as remote:
        remote.request_timeout_s = 0.1  # The client also must not use its short generic RPC deadline.
        session_id = uuid4()
        await register_on_fake_sandbox(engine, session_id)
        submitted = asyncio.create_task(remote.command(DESTINATION, str(session_id), command))
        delivered = await peer.next(runner_control_pb2.DeliverCommandRequest)
        # Elapsed time is the subject: the ack lands after both 0.1 s budgets but inside the 1 s ack budget.
        await asyncio.sleep(0.2)
        delivered.outcome.set_result(runner_control_pb2.DeliverCommandResponse(admission=admission(command, 1)))
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
    remote: RunnerGatewayClient, metadata: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as rejected:
        await remote.stub.ListSessions(
            runner_gateway_pb2.ListSessionsRequest(destination=DESTINATION), metadata=metadata
        )
    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"owner": sandboxes_pb2.ServiceAccount(namespace=SANDBOX_NAMESPACE, name="other")}, grpc.StatusCode.NOT_FOUND),
        ({"sandbox_uid": str(uuid4())}, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_destination_authority_and_incarnation(
    remote: RunnerGatewayClient, change: dict[str, object], code: grpc.StatusCode
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


@pytest.mark.parametrize("account_name", [ACCOUNT, "unlisted-service"])
async def test_unlisted_callers_rejected_before_lookup(
    resources: Resources, token_file: Path, cluster: SandboxCluster, account_name: str
) -> None:
    # Owning the destination does not grant gateway access. The allowlist gates every RPC.
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
    destination = sandboxes_pb2.SessionDestination(sandbox=DESTINATION, session_id="session")
    async with service(configured) as target, dial(target) as channel:
        remote = gateway_client(channel, token_file, command_admission_timeout_s=10, control_target=target)
        for gateway_call, gateway_request in (
            (remote.stub.ListSessions, runner_gateway_pb2.ListSessionsRequest(destination=DESTINATION)),
            (remote.stub.ResumeSession, runner_gateway_pb2.ResumeSessionRequest(destination=destination)),
            (remote.stub.SubmitCommand, runner_gateway_pb2.SubmitCommandRequest(destination=destination)),
        ):
            with pytest.raises(ServiceError) as rejected:
                await remote.unary(gateway_call, gateway_request)
            assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(ServiceError) as rejected:
            await remote.runner(DESTINATION).list_sessions()
        assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert cluster.fake.pod_reads == 0


@pytest.mark.parametrize(
    "command",
    [
        command_pb2.Command(),
        command_pb2.Command(command_id="missing-operation"),
        command_pb2.Command(submit_input=command_pb2.SubmitInput(text="missing-id")),
    ],
)
async def test_invalid_command_is_not_submitted(
    remote: RunnerGatewayClient, cluster: SandboxCluster, command: command_pb2.Command
) -> None:
    with pytest.raises(ServiceError) as rejected:
        await remote.command(DESTINATION, str(uuid4()), command)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert cluster.fake.pod_reads == 0


@pytest.mark.parametrize("field", ["controller", "uid", "name", "kind", "apiVersion"])
async def test_unverified_pod_owner_is_unavailable(
    remote: RunnerGatewayClient, peer: ScriptedRunner, cluster: SandboxCluster, field: str
) -> None:
    cluster.fake.pods[SANDBOX]["metadata"]["ownerReferences"][0][field] = (
        False if field == "controller" else "test-foreign"
    )
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert not peer.received


@pytest.mark.parametrize("condition", ["different-account", "suspended", "sealed-and-deleting"])
async def test_unavailable_destination_does_not_contact_runner(
    remote: RunnerGatewayClient, peer: ScriptedRunner, cluster: SandboxCluster, condition: str
) -> None:
    stored = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    match condition:
        case "different-account":
            cluster.fake.pods[SANDBOX]["spec"]["serviceAccountName"] = "test-other"
        case "suspended":
            stored["spec"]["running"] = False
            # The desired stop is not effective until the controller reports it and clears its incarnation.
            stored["status"]["operatingMode"] = "Suspended"
            stored["status"].pop("incarnation", None)
        case "sealed-and-deleting":
            stored["metadata"]["deletionTimestamp"] = "2026-09-01T12:00:00Z"
            stored["status"]["conditions"] = [
                {
                    "type": QUIESCED,
                    "status": "True",
                    "reason": "Sealed",
                    "message": "",
                    "lastTransitionTime": "2026-09-01T12:00:00Z",
                }
            ]
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert not peer.received


async def test_session_sandboxes_answer_from_the_registry_including_deleted_sandboxes(
    cluster: SandboxCluster, resources: Resources, token_file: Path, engine: AsyncEngine
) -> None:
    live_id, deleted_id, unknown_id = uuid4(), uuid4(), uuid4()
    await register_on_fake_sandbox(engine, live_id)
    await register_session(
        engine, deleted_id, sandbox_namespace=SANDBOX_NAMESPACE, sandbox_name="Deleted-Sandbox", sandbox_uid=None
    )
    live = runner_gateway_pb2.SessionSandbox(
        session_id=str(live_id), sandbox_namespace=SANDBOX_NAMESPACE, sandbox=SANDBOX, sandbox_uid=SANDBOX_UID
    )
    deleted = runner_gateway_pb2.SessionSandbox(
        session_id=str(deleted_id), sandbox_namespace=SANDBOX_NAMESPACE, sandbox="Deleted-Sandbox"
    )
    async with service_client(resources, token_file) as remote:
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
