"""Lifecycle requests and commands over the runner control stream: a scripted runner on one replica's
channel listener, the public API on that replica and another, a fake API server and a migrated
database."""

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

from agentplane.grpc_calls import ServiceError
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.proto.sandbox_service_pb2 import TeardownPhase
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import (
    ConnectionNotHeldError,
    ControlProtocolError,
    ControlRefusedError,
    LifecycleControl,
)
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.grpc_service import connect, service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import (
    HELLO,
    OTHER_REPLICA,
    REPLICA,
    RUNNER_TOKEN,
    attached,
    serve_replica_control,
)
from agentplane.runner_gateway.testing.sessions import register_on_fake_sandbox
from agentplane.sandbox_service.teardown import GatewaySealing, SealNotFlushedError, Teardown
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf

CALLER_AUDIENCE = "test-sandbox-service"
CALLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-caller")
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
    """Start a replica; replicas share the cluster and the database."""
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{CALLER.namespace}:{CALLER.name}",
        pod_name="test-caller-pod",
        pod_uid="test-caller-pod-uid",
        audiences=(CALLER_AUDIENCE,),
    )

    @asynccontextmanager
    async def start(holder: ConnectionHolder) -> AsyncIterator[Replica]:
        control = LifecycleControl(leases)
        resources = replace(
            build_resources(
                engine,
                principals=cluster.build_principal_resolver(CALLER_AUDIENCE),
                destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
                caller_accounts=frozenset({CALLER}),
                platform_instructions="Test platform guidance.",
            ),
            control=control,
            sealer_accounts=frozenset({CALLER}),
        )
        async with (
            serve_replica_control(
                cluster,
                holder,
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


@pytest.fixture
async def public_id(engine: AsyncEngine) -> UUID:
    """A Session registered on the fake cluster's Sandbox."""
    public_id = uuid4()
    await register_on_fake_sandbox(engine, public_id)
    return public_id


@asynccontextmanager
async def connected(replica: Replica) -> AsyncIterator[Connect]:
    """A scripted runner's `Connect` stream, welcomed."""
    call = replica.channel.Connect(metadata=(("authorization", f"Bearer {RUNNER_TOKEN}"),), timeout=30)
    try:
        await call.write(HELLO)
        frame = await call.read()
        assert isinstance(frame, runner_control_pb2.ServiceFrame)
        assert frame.HasField("welcome")
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


def client(token_file: Path, target: str, *, control_target: str) -> AbstractAsyncContextManager[RunnerGatewayClient]:
    return connect(target, token_file, command_admission_timeout_s=10, control_target=control_target)


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
    async with connected(holder) as call, client(token_file, holder.api, control_target=holder.api) as service:
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


async def test_listing_and_resuming_go_over_the_channel(holder: Replica, public_id: UUID, token_file: Path) -> None:
    async with connected(holder) as call, client(token_file, holder.api, control_target=holder.api) as service:
        runner = service.runner(DESTINATION)
        listing = asyncio.create_task(runner.list_sessions())
        listed = await next_request(call)
        assert listed.WhichOneof("verb") == "list_sessions"
        result = runner_control_pb2.ControlResult(request_id=listed.request_id)
        result.list_sessions.sessions.add(session_id=str(public_id), last_cursor=7)
        await answer(call, result)
        assert [(row.session_id, row.last_cursor) for row in await listing] == [(str(public_id), 7)]

        resuming = asyncio.create_task(runner.resume(str(public_id)))
        resume = await next_request(call)
        assert resume.resume.session_id == str(public_id)
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
    # A control Service still naming the other replica, as during a roll: the caller's retry resolves
    # the control Service again.
    async with connected(holder), client(token_file, other.api, control_target=other.api) as caller:
        with pytest.raises(ServiceError) as refused:
            await caller.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC)
        assert refused.value.code == grpc.StatusCode.UNAVAILABLE


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
    async with client(token_file, holder.api, control_target=holder.api) as service:
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


def sandbox_service_teardown(cluster: Cluster, gateway: RunnerGatewayClient) -> Teardown:
    return Teardown(cluster.inventory, GatewaySealing(gateway.control))


async def test_teardown_seals_over_the_holders_connection(
    holder: Replica, other: Replica, cluster: Cluster, public_id: UUID, token_file: Path
) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    view = await cluster.inventory.get(SANDBOX)
    await cluster.inventory.begin_teardown(SANDBOX, uid=view.uid)
    sealing = await cluster.inventory.get(SANDBOX)
    async with (
        connected(holder) as call,
        client(token_file, other.api, control_target=other.api) as unrouted,
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
        result.seal.sessions.add(session_id=str(public_id), cursor=3)
        await answer(call, result)
        # The holder's answer reached the Sandbox Service, which waits for the ingress.
        with pytest.raises(SealNotFlushedError):
            await advancing
    assert (await cluster.inventory.get(SANDBOX)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING


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


COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="test input"))


async def next_delivery(call: Connect) -> runner_control_pb2.DeliverCommand:
    """The next command delivery, answering heartbeats meanwhile."""
    async with asyncio.timeout(10):
        while (frame := await call.read()) is not grpc.aio.EOF:
            assert isinstance(frame, runner_control_pb2.ServiceFrame)
            if frame.HasField("deliver"):
                return frame.deliver
            await call.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
    raise AssertionError("the service ended the stream")


async def test_a_command_reaches_the_holder_and_returns_the_runner_admission(
    holder: Replica, other: Replica, public_id: UUID, token_file: Path
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.origin.source_id, admitted.origin.sequence = "test-runner-source", 1
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    # The caller's own target is the other replica; the control Service names the holder.
    async with connected(holder) as call, client(token_file, other.api, control_target=holder.api) as service:
        submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
        sent = await next_delivery(call)
        assert (sent.session_id, sent.command) == (str(public_id), COMMAND)
        await call.write(
            runner_control_pb2.RunnerFrame(
                receipt=runner_control_pb2.CommandReceipt(
                    session_id=sent.session_id, command_id=COMMAND.command_id, admitted=admitted
                )
            )
        )
        assert await submitting == admitted


async def test_a_refused_command_carries_its_reason(holder: Replica, public_id: UUID, token_file: Path) -> None:
    async with connected(holder) as call, client(token_file, holder.api, control_target=holder.api) as service:
        submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
        sent = await next_delivery(call)
        await call.write(
            runner_control_pb2.RunnerFrame(
                receipt=runner_control_pb2.CommandReceipt(
                    session_id=sent.session_id,
                    command_id=COMMAND.command_id,
                    refused=runner_control_pb2.CommandRefused(
                        reason=runner_control_pb2.CommandRefused.REASON_COMMAND_CONFLICT
                    ),
                )
            )
        )
        with pytest.raises(ServiceError) as refused:
            await submitting
        assert refused.value.code == grpc.StatusCode.ALREADY_EXISTS


async def test_a_lost_connection_fails_its_command_and_a_retry_delivers_it_again(
    holder: Replica, public_id: UUID, token_file: Path
) -> None:
    async with client(token_file, holder.api, control_target=holder.api) as service:
        async with connected(holder) as call:
            submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
            await next_delivery(call)
        with pytest.raises(ServiceError) as lost:
            await submitting
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE
        async with connected(holder) as call:
            retrying = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
            assert (await next_delivery(call)).command == COMMAND
            retrying.cancel()


if __name__ == "__main__":
    pytest_bazel.main()
