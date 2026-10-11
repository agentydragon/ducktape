"""Lifecycle requests over the runner channel: a scripted runner on one replica's channel listener,
the public API on that replica and another, a fake API server and a migrated commands database."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError
from agentplane.runner_channel import protocol_pb2, protocol_pb2_grpc
from agentplane.sandbox_service.channel_control import (
    ChannelControl,
    ConnectionNotHeldError,
    ControlProtocolError,
    ControlRefusedError,
)
from agentplane.sandbox_service.channel_delivery import ChannelDelivery
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.commands.connections import ConnectionStore
from agentplane.sandbox_service.commands.dispatch import DispatchStore
from agentplane.sandbox_service.commands.store import SubmissionStore
from agentplane.sandbox_service.connection_records import ConnectionHolder, ConnectionLeases, ConnectionRecords
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, TeardownPhase
from agentplane.sandbox_service.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.session_history.session_changes import SessionChanges
from agentplane.sandbox_service.session_registry import SessionRegistry
from agentplane.sandbox_service.teardown import Teardown
from agentplane.sandbox_service.testing.grpc_service import service
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.sandbox_service.testing.sessions import register_session
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/commands:conftest

pytest_plugins = ("agentplane.sandbox_service.commands.conftest",)

RUNNER_AUDIENCE = "test-runner-channel"
RUNNER_TOKEN = "test-runner-token"
CALLER_AUDIENCE = "test-sandbox-service"
CALLER_TOKEN = "test-caller-token"
CALLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-caller")
HELLO = protocol_pb2.RunnerFrame(
    hello=protocol_pb2.Hello(runner_boot_id="test-boot", capabilities=["commands", "lifecycle"])
)
HOLDER = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)
SPEC = {"harness": "HARNESS_CLAUDE", "cwd": "/test-workspace/{session_id}", "model": "test-model"}
Reason = protocol_pb2.ControlRefused.Reason

type Connect = grpc.aio.StreamStreamCall[protocol_pb2.RunnerFrame, protocol_pb2.ServiceFrame]


@dataclass
class Replica:
    """One Sandbox Service replica: its channel listener, its public API and its lifecycle sender."""

    channel: protocol_pb2_grpc.RunnerChannelAsyncStub
    api: str
    control: ChannelControl
    teardown: Teardown


@pytest.fixture
async def leases(cluster: Cluster) -> AsyncIterator[ConnectionLeases]:
    leases = ConnectionLeases()
    watch = asyncio.create_task(
        leases.watch(k8s_client.CoordinationV1Api(cluster.api), SANDBOX_NAMESPACE, resync_seconds=60).run()
    )
    try:
        yield leases
    finally:
        watch.cancel()
        with suppress(asyncio.CancelledError):
            await watch


@pytest.fixture
def replica(
    cluster: Cluster, engine: AsyncEngine, leases: ConnectionLeases
) -> Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]:
    """Start a replica; replicas share the cluster, the database and its agreed operations."""
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{CALLER.namespace}:{CALLER.name}",
        pod_name="test-caller-pod",
        pod_uid="test-caller-pod-uid",
        audiences=(CALLER_AUDIENCE,),
    )
    connections = ConnectionStore(engine)

    @asynccontextmanager
    async def start(holder: ConnectionHolder) -> AsyncIterator[Replica]:
        control = ChannelControl(leases)
        destinations = DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), 1)
        delivery = ChannelDelivery(
            engine.url, dispatches=DispatchStore(engine), connections=connections, leases=leases, liveness_s=5
        )
        channel_server = grpc.aio.server()
        protocol_pb2_grpc.add_RunnerChannelServicer_to_server(
            RunnerChannelService(
                principals=WorkloadPrincipalResolver(
                    authentication=k8s_client.AuthenticationV1Api(cluster.api),
                    audience=RUNNER_AUDIENCE,
                    allowed_service_account_namespaces={SANDBOX_NAMESPACE},
                ),
                inventory=cluster.inventory,
                core=k8s_client.CoreV1Api(cluster.api),
                connections=connections,
                records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
                delivery=delivery,
                control=control,
                timing=ChannelTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1, max_age_s=60),
            ),
            channel_server,
        )
        port = channel_server.add_insecure_port("127.0.0.1:0")
        await channel_server.start()
        resources = Resources(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(cluster.api),
                audience=CALLER_AUDIENCE,
                allowed_service_account_namespaces={SANDBOX_NAMESPACE},
            ),
            destinations=destinations,
            provisioning=cluster.provisioning,
            caller_accounts=frozenset({CALLER}),
            platform_instructions="Test platform guidance.",
            runner_admission_ack_timeout_s=1,
            sessions=SessionRegistry(engine),
            submissions=SubmissionStore(engine),
            delivery=delivery,
            control=control,
            session_changes=SessionChanges(engine.url),
        )
        try:
            async with service(resources) as api, grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                yield Replica(
                    channel=protocol_pb2_grpc.RunnerChannelStub(channel),
                    api=api,
                    control=control,
                    teardown=Teardown(
                        destinations,
                        SessionRegistry(engine),
                        connections=connections,
                        control=control,
                        runner_grpc_channel_options={},
                    ),
                )
        finally:
            await channel_server.stop(0)

    return start


@pytest.fixture
async def holder(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(HOLDER) as started:
        yield started


@pytest.fixture
async def other(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(OTHER) as started:
        yield started


@asynccontextmanager
async def connected(replica: Replica) -> AsyncIterator[Connect]:
    """A scripted runner's `Connect` stream, welcomed."""
    call = replica.channel.Connect(metadata=(("authorization", f"Bearer {RUNNER_TOKEN}"),), timeout=30)
    try:
        await call.write(HELLO)
        frame = await call.read()
        assert isinstance(frame, protocol_pb2.ServiceFrame)
        assert list(frame.welcome.capabilities) == ["commands", "lifecycle"]
        yield call
    finally:
        call.cancel()


async def next_request(call: Connect) -> protocol_pb2.ControlRequest:
    """The next control request, answering heartbeats meanwhile."""
    async with asyncio.timeout(10):
        while (frame := await call.read()) is not grpc.aio.EOF:
            assert isinstance(frame, protocol_pb2.ServiceFrame)
            if frame.HasField("control"):
                return frame.control
            await call.write(protocol_pb2.RunnerFrame(heartbeat=protocol_pb2.Heartbeat()))
    raise AssertionError("the service ended the stream")


async def answer(call: Connect, result: protocol_pb2.ControlResult) -> None:
    await call.write(protocol_pb2.RunnerFrame(control_result=result))


def attached(session_id: str, harness_state: runner_pb2.HarnessState) -> runner_pb2.Attached:
    return runner_pb2.Attached(
        session_id=session_id, harness_state=harness_state, setup_state=runner_pb2.SETUP_STATE_NOT_REQUIRED
    )


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "caller-token"
    path.write_text(CALLER_TOKEN)
    return path


@pytest.fixture
async def destination(cluster: Cluster) -> SandboxDestination:
    view = await cluster.inventory.get(SANDBOX)
    return SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=view.uid)


@asynccontextmanager
async def client(token_file: Path, target: str, *, control_target: str | None) -> AsyncIterator[SandboxServiceClient]:
    started = SandboxServiceClient(
        target,
        namespace=SANDBOX_NAMESPACE,
        token_file=token_file,
        command_admission_timeout_s=None,
        request_timeout_s=20,
        lifecycle_timeout_s=20,
        follow_timeout_s=20,
        control_target=control_target,
    )
    try:
        yield started
    finally:
        await started.close()


async def test_a_create_whose_harness_failed_resumes_the_session_on_retry(
    holder: Replica, other: Replica, token_file: Path, destination: SandboxDestination
) -> None:
    # The caller's own target is the other replica; the control Service names the holder.
    async with connected(holder) as call, client(token_file, other.api, control_target=holder.api) as service:
        runner = service.runner(destination)
        first = asyncio.create_task(runner.create(idempotency_key="test-open", spec=SPEC))
        opened = await next_request(call)
        assert opened.WhichOneof("verb") == "open"
        session_id = opened.open.session_id
        await answer(
            call,
            protocol_pb2.ControlResult(
                request_id=opened.request_id,
                refused=protocol_pb2.ControlRefused(reason=Reason.REASON_FAILED, detail="test launch failure"),
            ),
        )
        with pytest.raises(RunnerError):
            await first
        # The runner created the Session and could not start it; asked again it only confirms it,
        # stopped, so the create starts it.
        retried = asyncio.create_task(runner.create(idempotency_key="test-open", spec=SPEC))
        confirmed = await next_request(call)
        assert (confirmed.open.session_id, confirmed.open.spec) == (session_id, opened.open.spec)
        await answer(
            call,
            protocol_pb2.ControlResult(
                request_id=confirmed.request_id, open=attached(session_id, runner_pb2.HARNESS_STATE_STOPPED)
            ),
        )
        resumed = await next_request(call)
        assert resumed.resume.session_id == session_id
        await answer(
            call,
            protocol_pb2.ControlResult(
                request_id=resumed.request_id, resume=attached(session_id, runner_pb2.HARNESS_STATE_RUNNING)
            ),
        )
        response = await retried
    assert response.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert response.attached.session_id == response.session_id
    assert session_id == f"r-{response.session_id}"


async def test_a_created_running_session_is_not_resumed(
    holder: Replica, token_file: Path, destination: SandboxDestination
) -> None:
    async with connected(holder) as call, client(token_file, holder.api, control_target=None) as service:
        creating = asyncio.create_task(service.runner(destination).create(idempotency_key="test-open", spec=SPEC))
        opened = await next_request(call)
        await answer(
            call,
            protocol_pb2.ControlResult(
                request_id=opened.request_id, open=attached(opened.open.session_id, runner_pb2.HARNESS_STATE_RUNNING)
            ),
        )
        # Waiting on a resume would never return.
        assert (await creating).attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING


async def test_listing_and_resuming_go_over_the_channel(
    holder: Replica, engine: AsyncEngine, token_file: Path, destination: SandboxDestination
) -> None:
    public_id = uuid4()
    await register_session(
        engine,
        public_id,
        sandbox_namespace=SANDBOX_NAMESPACE,
        sandbox_name=SANDBOX,
        sandbox_uid=UUID(SANDBOX_UID),
        runner_session_id="test-runner-session",
    )
    async with connected(holder) as call, client(token_file, holder.api, control_target=None) as service:
        runner = service.runner(destination)
        listing = asyncio.create_task(runner.list_sessions())
        listed = await next_request(call)
        assert listed.WhichOneof("verb") == "list_sessions"
        result = protocol_pb2.ControlResult(request_id=listed.request_id)
        result.list_sessions.sessions.add(session_id="test-runner-session", last_cursor=7)
        await answer(call, result)
        assert [(row.session_id, row.last_cursor) for row in await listing] == [(str(public_id), 7)]

        resuming = asyncio.create_task(runner.resume(str(public_id)))
        resume = await next_request(call)
        assert resume.resume.session_id == "test-runner-session"
        await answer(
            call,
            protocol_pb2.ControlResult(
                request_id=resume.request_id,
                refused=protocol_pb2.ControlRefused(reason=Reason.REASON_SETUP_FAILED, detail="test setup failed"),
            ),
        )
        with pytest.raises(RunnerError):
            await resuming


async def test_a_replica_without_the_connection_refuses_before_acting(
    holder: Replica, other: Replica, token_file: Path, destination: SandboxDestination
) -> None:
    async with connected(holder):
        async with client(token_file, other.api, control_target=None) as direct:
            with pytest.raises(ServiceError) as refused:
                await direct.runner(destination).create(idempotency_key="test-open", spec=SPEC)
            assert refused.value.code == grpc.StatusCode.UNAVAILABLE
        # A control Service still naming the other replica, as during a roll: the call is refused
        # there and served by the holder.
        async with connected(holder) as call, client(token_file, holder.api, control_target=other.api) as stale:
            listing = asyncio.create_task(stale.runner(destination).list_sessions())
            listed = await next_request(call)
            await answer(call, protocol_pb2.ControlResult(request_id=listed.request_id, list_sessions={}))
            assert await listing == []


async def test_a_superseded_connection_sends_nothing(holder: Replica, other: Replica, leases: ConnectionLeases) -> None:
    async with connected(holder), connected(other):
        async with asyncio.timeout(5):
            await leases.wait_for(lambda: leases.read_epoch(UUID(SANDBOX_UID)) == 2)
        with pytest.raises(ConnectionNotHeldError):
            await holder.control.request(
                UUID(SANDBOX_UID), protocol_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
            )


async def test_a_lost_connection_drops_its_request_and_a_reconnect_replays_none(
    holder: Replica, token_file: Path, destination: SandboxDestination
) -> None:
    async with client(token_file, holder.api, control_target=None) as service:
        async with connected(holder) as call:
            listing = asyncio.create_task(service.runner(destination).list_sessions())
            await next_request(call)
        with pytest.raises(ServiceError) as lost:
            await listing
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE
        async with connected(holder) as call:
            for _ in range(3):
                frame = await call.read()
                assert isinstance(frame, protocol_pb2.ServiceFrame)
                assert frame.HasField("heartbeat")


async def test_an_answer_for_another_verb_fails_the_request(holder: Replica) -> None:
    async with connected(holder) as call:
        listing = asyncio.create_task(
            holder.control.request(
                UUID(SANDBOX_UID), protocol_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
            )
        )
        listed = await next_request(call)
        await answer(call, protocol_pb2.ControlResult(request_id=listed.request_id, seal={}))
        with pytest.raises(ControlProtocolError):
            await listing


async def test_teardown_seals_over_the_holders_connection(holder: Replica, other: Replica, cluster: Cluster) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    view = await cluster.inventory.get(SANDBOX)
    await cluster.inventory.begin_teardown(SANDBOX, uid=view.uid)
    sealing = await cluster.inventory.get(SANDBOX)
    async with connected(holder) as call:
        # Every replica reconciles the Sandbox; only the holder seals it.
        with pytest.raises(ConnectionNotHeldError):
            await other.teardown.advance(sealing)
        advancing = asyncio.create_task(holder.teardown.advance(sealing))
        sealed = await next_request(call)
        assert sealed.WhichOneof("verb") == "seal"
        result = protocol_pb2.ControlResult(request_id=sealed.request_id)
        result.seal.sessions.add(session_id="test-runner-session", cursor=3)
        await answer(call, result)
        await advancing
    assert (await cluster.inventory.get(SANDBOX)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALED


async def test_a_refusal_carries_its_reason(holder: Replica) -> None:
    async with connected(holder) as call:
        resuming = asyncio.create_task(
            holder.control.request(
                UUID(SANDBOX_UID),
                protocol_pb2.ControlRequest(resume=protocol_pb2.ResumeSession(session_id="test-unknown")),
            )
        )
        resume = await next_request(call)
        refusal = protocol_pb2.ControlRefused(reason=Reason.REASON_SESSION_UNKNOWN, detail="test unknown")
        await answer(call, protocol_pb2.ControlResult(request_id=resume.request_id, refused=refusal))
        with pytest.raises(ControlRefusedError) as refused:
            await resuming
        assert refused.value.refusal == refusal


if __name__ == "__main__":
    pytest_bazel.main()
