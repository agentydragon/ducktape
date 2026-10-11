"""Lifecycle requests over the runner control stream: a scripted runner on one replica's channel listener,
the public API on that replica and another, a fake API server and a migrated commands database."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.proto.sandbox_service_pb2 import TeardownPhase
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases
from agentplane.runner_gateway.lifecycle_control import (
    ConnectionNotHeldError,
    ControlProtocolError,
    ControlRefusedError,
    LifecycleControl,
)
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.grpc_service import service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import OTHER_REPLICA, REPLICA, RUNNER_TOKEN, serve_channel
from agentplane.runner_gateway.testing.sessions import register_session
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.teardown import GatewaySealing, Teardown
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN, connect
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf

CALLER_AUDIENCE = "test-sandbox-service"
CALLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-caller")
HELLO = runner_control_pb2.RunnerFrame(
    hello=runner_control_pb2.Hello(runner_boot_id="test-boot", capabilities=["commands", "lifecycle"])
)
SPEC: dict[str, object] = {"harness": "HARNESS_CLAUDE", "cwd": "/test-workspace/{session_id}", "model": "test-model"}
Reason = runner_control_pb2.ControlRefused.Reason

type Connect = grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame]


@dataclass
class Replica:
    """One gateway replica: its channel listener, its public API and its lifecycle sender."""

    channel: runner_control_pb2_grpc.RunnerControlAsyncStub
    api: str
    control: LifecycleControl


@pytest.fixture
def replica(
    cluster: Cluster, engine: AsyncEngine, leases: ConnectionLeases
) -> Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]:
    """Start a replica; replicas share the cluster, the database and its agreed operations."""
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{CALLER.namespace}:{CALLER.name}",
        pod_name="test-caller-pod",
        pod_uid="test-caller-pod-uid",
        audiences=(CALLER_AUDIENCE,),
    )
    connections = ConnectionStore(engine)

    @asynccontextmanager
    async def start(holder: ConnectionHolder) -> AsyncIterator[Replica]:
        control = LifecycleControl(leases)
        delivery = CommandDelivery(
            engine.url, dispatches=DispatchStore(engine), connections=connections, leases=leases, liveness_s=5
        )
        resources = replace(
            build_resources(
                engine,
                principals=cluster.build_principal_resolver(CALLER_AUDIENCE),
                destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), 1),
                caller_accounts=frozenset({CALLER}),
                platform_instructions="Test platform guidance.",
            ),
            delivery=delivery,
            control=control,
            sealer_accounts=frozenset({CALLER}),
        )
        async with (
            serve_channel(
                cluster,
                holder,
                connections=connections,
                delivery=delivery,
                control=control,
                timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1, max_age_s=60),
            ) as channel,
            service(resources) as api,
        ):
            yield Replica(channel=channel, api=api, control=control)

    return start


@pytest.fixture
async def holder(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(REPLICA) as started:
        yield started


@pytest.fixture
async def other(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(OTHER_REPLICA) as started:
        yield started


@asynccontextmanager
async def connected(replica: Replica) -> AsyncIterator[Connect]:
    """A scripted runner's `Connect` stream, welcomed."""
    call = replica.channel.Connect(metadata=(("authorization", f"Bearer {RUNNER_TOKEN}"),), timeout=30)
    try:
        await call.write(HELLO)
        frame = await call.read()
        assert isinstance(frame, runner_control_pb2.ServiceFrame)
        assert list(frame.welcome.capabilities) == ["commands", "lifecycle"]
        yield call
    finally:
        call.cancel()


async def next_request(call: Connect) -> runner_control_pb2.ControlRequest:
    """The next control request, answering heartbeats meanwhile."""
    async with asyncio.timeout(10):
        while (frame := await call.read()) is not grpc.aio.EOF:
            assert isinstance(frame, runner_control_pb2.ServiceFrame)
            if frame.HasField("control"):
                return frame.control
            await call.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
    raise AssertionError("the service ended the stream")


async def answer(call: Connect, result: runner_control_pb2.ControlResult) -> None:
    await call.write(runner_control_pb2.RunnerFrame(control_result=result))


def attached(session_id: str, harness_state: runner_pb2.HarnessState) -> runner_pb2.Attached:
    return runner_pb2.Attached(
        session_id=session_id, harness_state=harness_state, setup_state=runner_pb2.SETUP_STATE_NOT_REQUIRED
    )


def client(token_file: Path, target: str, *, control_target: str | None) -> SandboxServiceClient:
    return connect(
        target, token_file, namespace=SANDBOX_NAMESPACE, command_admission_timeout_s=None, control_target=control_target
    )


async def test_a_create_whose_harness_failed_resumes_the_session_on_retry(
    holder: Replica, other: Replica, token_file: Path
) -> None:
    # The caller's own target is the other replica; the control Service names the holder.
    async with connected(holder) as call, client(token_file, other.api, control_target=holder.api) as service:
        runner = service.runner(DESTINATION)
        requested = uuid4()
        first = asyncio.create_task(runner.create(session_id=requested, spec=SPEC))
        opened = await next_request(call)
        assert opened.WhichOneof("verb") == "open"
        session_id = opened.open.session_id
        await answer(
            call,
            runner_control_pb2.ControlResult(
                request_id=opened.request_id,
                refused=runner_control_pb2.ControlRefused(reason=Reason.REASON_FAILED, detail="test launch failure"),
            ),
        )
        with pytest.raises(RunnerError):
            await first
        # The runner created the Session and could not start it; asked again it only confirms it,
        # stopped, so the create starts it.
        retried = asyncio.create_task(runner.create(session_id=requested, spec=SPEC))
        confirmed = await next_request(call)
        assert (confirmed.open.session_id, confirmed.open.spec) == (session_id, opened.open.spec)
        await answer(
            call,
            runner_control_pb2.ControlResult(
                request_id=confirmed.request_id, open=attached(session_id, runner_pb2.HARNESS_STATE_STOPPED)
            ),
        )
        resumed = await next_request(call)
        assert resumed.resume.session_id == session_id
        await answer(
            call,
            runner_control_pb2.ControlResult(
                request_id=resumed.request_id, resume=attached(session_id, runner_pb2.HARNESS_STATE_RUNNING)
            ),
        )
        response = await retried
    assert response.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert response.attached.session_id == response.session_id
    assert session_id == response.session_id == str(requested)


async def test_a_created_running_session_is_not_resumed(holder: Replica, token_file: Path) -> None:
    async with connected(holder) as call, client(token_file, holder.api, control_target=None) as service:
        creating = asyncio.create_task(service.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC))
        opened = await next_request(call)
        await answer(
            call,
            runner_control_pb2.ControlResult(
                request_id=opened.request_id, open=attached(opened.open.session_id, runner_pb2.HARNESS_STATE_RUNNING)
            ),
        )
        # Waiting on a resume would never return.
        assert (await creating).attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING


async def test_listing_and_resuming_go_over_the_channel(holder: Replica, engine: AsyncEngine, token_file: Path) -> None:
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
        runner = service.runner(DESTINATION)
        listing = asyncio.create_task(runner.list_sessions())
        listed = await next_request(call)
        assert listed.WhichOneof("verb") == "list_sessions"
        result = runner_control_pb2.ControlResult(request_id=listed.request_id)
        result.list_sessions.sessions.add(session_id="test-runner-session", last_cursor=7)
        await answer(call, result)
        assert [(row.session_id, row.last_cursor) for row in await listing] == [(str(public_id), 7)]

        resuming = asyncio.create_task(runner.resume(str(public_id)))
        resume = await next_request(call)
        assert resume.resume.session_id == "test-runner-session"
        await answer(
            call,
            runner_control_pb2.ControlResult(
                request_id=resume.request_id,
                refused=runner_control_pb2.ControlRefused(
                    reason=Reason.REASON_SETUP_FAILED, detail="test setup failed"
                ),
            ),
        )
        with pytest.raises(RunnerError):
            await resuming


async def test_a_replica_without_the_connection_refuses_before_acting(
    holder: Replica, other: Replica, token_file: Path
) -> None:
    async with connected(holder):
        async with client(token_file, other.api, control_target=None) as direct:
            with pytest.raises(ServiceError) as refused:
                await direct.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC)
            assert refused.value.code == grpc.StatusCode.UNAVAILABLE
        # A control Service still naming the other replica, as during a roll: the call is refused
        # there and served by the holder.
        async with connected(holder) as call, client(token_file, holder.api, control_target=other.api) as stale:
            listing = asyncio.create_task(stale.runner(DESTINATION).list_sessions())
            listed = await next_request(call)
            await answer(call, runner_control_pb2.ControlResult(request_id=listed.request_id, list_sessions={}))
            assert await listing == []


async def test_a_superseded_connection_sends_nothing(holder: Replica, other: Replica, leases: ConnectionLeases) -> None:
    async with connected(holder), connected(other):
        async with asyncio.timeout(5):
            await leases.wait_for(lambda: leases.read_epoch(UUID(SANDBOX_UID)) == 2)
        with pytest.raises(ConnectionNotHeldError):
            await holder.control.request(
                UUID(SANDBOX_UID), runner_control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
            )


async def test_a_lost_connection_drops_its_request_and_a_reconnect_replays_none(
    holder: Replica, token_file: Path
) -> None:
    async with client(token_file, holder.api, control_target=None) as service:
        async with connected(holder) as call:
            listing = asyncio.create_task(service.runner(DESTINATION).list_sessions())
            await next_request(call)
        with pytest.raises(ServiceError) as lost:
            await listing
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE
        async with connected(holder) as call:
            for _ in range(3):
                frame = await call.read()
                assert isinstance(frame, runner_control_pb2.ServiceFrame)
                assert frame.HasField("heartbeat")


async def test_an_answer_for_another_verb_fails_the_request(holder: Replica) -> None:
    async with connected(holder) as call:
        listing = asyncio.create_task(
            holder.control.request(
                UUID(SANDBOX_UID), runner_control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
            )
        )
        listed = await next_request(call)
        await answer(call, runner_control_pb2.ControlResult(request_id=listed.request_id, seal={}))
        with pytest.raises(ControlProtocolError):
            await listing


def sandbox_service_teardown(cluster: Cluster, gateway: SandboxServiceClient) -> Teardown:
    return Teardown(cluster.inventory, GatewaySealing(gateway))


async def test_teardown_seals_over_the_holders_connection(
    holder: Replica, other: Replica, cluster: Cluster, token_file: Path
) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    view = await cluster.inventory.get(SANDBOX)
    await cluster.inventory.begin_teardown(SANDBOX, uid=view.uid)
    sealing = await cluster.inventory.get(SANDBOX)
    async with (
        connected(holder) as call,
        client(token_file, other.api, control_target=None) as unrouted,
        client(token_file, other.api, control_target=holder.api) as routed,
    ):
        # Only the holder seals: a replica without the connection refuses, and the Sandbox Service's
        # call through the control Service reaches the holder.
        with pytest.raises(ServiceError) as refused:
            await sandbox_service_teardown(cluster, unrouted).advance(sealing)
        assert refused.value.code == grpc.StatusCode.UNAVAILABLE
        advancing = asyncio.create_task(sandbox_service_teardown(cluster, routed).advance(sealing))
        sealed = await next_request(call)
        assert sealed.WhichOneof("verb") == "seal"
        result = runner_control_pb2.ControlResult(request_id=sealed.request_id)
        result.seal.sessions.add(session_id="test-runner-session", cursor=3)
        await answer(call, result)
        await advancing
    assert (await cluster.inventory.get(SANDBOX)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALED


async def test_a_refusal_carries_its_reason(holder: Replica) -> None:
    async with connected(holder) as call:
        resuming = asyncio.create_task(
            holder.control.request(
                UUID(SANDBOX_UID),
                runner_control_pb2.ControlRequest(resume=runner_control_pb2.ResumeSession(session_id="test-unknown")),
            )
        )
        resume = await next_request(call)
        refusal = runner_control_pb2.ControlRefused(reason=Reason.REASON_SESSION_UNKNOWN, detail="test unknown")
        await answer(call, runner_control_pb2.ControlResult(request_id=resume.request_id, refused=refusal))
        with pytest.raises(ControlRefusedError) as refused:
            await resuming
        assert refused.value.refusal == refusal


if __name__ == "__main__":
    pytest_bazel.main()
