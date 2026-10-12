"""Lifecycle calls and commands to the runner's `Runner` service over its control stream: a scripted
runner on one replica's channel listener, the public API on that replica and another, a fake API server
and a migrated database."""

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

from agentplane.grpc_calls import RefusedError, ServiceError
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_pb2
from agentplane.proto.sandbox_service_pb2 import TeardownPhase
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.grpc_service import connect, service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import (
    OTHER_REPLICA,
    REPLICA,
    ScriptedRunner,
    attached,
    connected,
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


@dataclass
class Replica:
    """One gateway replica: its channel listener's target, its public API and its runner connections."""

    listener: str
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
            ) as listener,
            service(resources) as api,
        ):
            yield Replica(listener=listener, api=api, control=control)

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
async def runner_on(replica: Replica) -> AsyncIterator[ScriptedRunner]:
    """A scripted runner holding the Sandbox's connection to `replica`."""
    runner = ScriptedRunner()
    async with connected(replica.listener, runner, replica.control):
        yield runner


def client(token_file: Path, target: str, *, control_target: str) -> AbstractAsyncContextManager[RunnerGatewayClient]:
    return connect(target, token_file, command_admission_timeout_s=10, control_target=control_target)


async def test_a_create_whose_harness_failed_resumes_the_session_on_retry(
    holder: Replica, other: Replica, token_file: Path
) -> None:
    # The caller's own target is the other replica; the control Service names the holder.
    async with runner_on(holder) as runner, client(token_file, other.api, control_target=holder.api) as service:
        sandbox = service.runner(DESTINATION)
        requested = uuid4()
        first = asyncio.create_task(sandbox.create(session_id=requested, spec=SPEC))
        opened = await runner.next(runner_control_pb2.OpenSessionRequest)
        session_id = opened.request.session_id
        # A harness that failed to launch.
        opened.outcome.set_result(grpc.StatusCode.UNKNOWN)
        with pytest.raises(RefusedError):
            await first
        # The runner created the Session and could not start it; asked again it only confirms it,
        # stopped, so the create starts it.
        retried = asyncio.create_task(sandbox.create(session_id=requested, spec=SPEC))
        confirmed = await runner.next(runner_control_pb2.OpenSessionRequest)
        assert (confirmed.request.session_id, confirmed.request.spec) == (session_id, opened.request.spec)
        confirmed.outcome.set_result(
            runner_control_pb2.OpenSessionResponse(attached=attached(session_id, runner_pb2.HARNESS_STATE_STOPPED))
        )
        resumed = await runner.next(runner_control_pb2.ResumeSessionRequest)
        assert resumed.request.session_id == session_id
        resumed.outcome.set_result(
            runner_control_pb2.ResumeSessionResponse(attached=attached(session_id, runner_pb2.HARNESS_STATE_RUNNING))
        )
        response = await retried
    assert response.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert response.attached.session_id == response.session_id
    assert session_id == response.session_id == str(requested)


async def test_a_created_running_session_is_not_resumed(holder: Replica, token_file: Path) -> None:
    async with runner_on(holder) as runner, client(token_file, holder.api, control_target=holder.api) as service:
        creating = asyncio.create_task(service.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC))
        await runner.open()
        # Waiting on a resume would never return.
        assert (await creating).attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING


async def test_listing_and_resuming_go_over_the_channel(holder: Replica, public_id: UUID, token_file: Path) -> None:
    async with runner_on(holder) as runner, client(token_file, holder.api, control_target=holder.api) as service:
        sandbox = service.runner(DESTINATION)
        runner.sessions[str(public_id)] = runner_pb2.SessionSummary(session_id=str(public_id), last_cursor=7)
        assert [(row.session_id, row.last_cursor) for row in await sandbox.list_sessions()] == [(str(public_id), 7)]

        resuming = asyncio.create_task(sandbox.resume(str(public_id)))
        resume = await runner.next(runner_control_pb2.ResumeSessionRequest)
        assert resume.request.session_id == str(public_id)
        # A Session whose setup failed.
        resume.outcome.set_result(grpc.StatusCode.FAILED_PRECONDITION)
        with pytest.raises(RefusedError):
            await resuming


async def test_a_replica_without_the_connection_refuses_before_acting(
    holder: Replica, other: Replica, token_file: Path
) -> None:
    # A control Service still naming the other replica, as during a roll: the caller's retry resolves
    # the control Service again.
    async with runner_on(holder) as runner, client(token_file, other.api, control_target=other.api) as caller:
        with pytest.raises(ServiceError) as refused:
            await caller.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC)
        assert refused.value.code == grpc.StatusCode.UNAVAILABLE
    assert not runner.received


async def test_a_superseded_connection_calls_nothing(holder: Replica, other: Replica, leases: ConnectionLeases) -> None:
    async with runner_on(holder), runner_on(other):
        async with asyncio.timeout(5):
            await leases.wait_for(lambda: leases.read_epoch(UUID(SANDBOX_UID)) == 2)
        with pytest.raises(ConnectionNotHeldError):
            holder.control.runner(UUID(SANDBOX_UID))


async def test_a_lost_connection_fails_its_call(holder: Replica, token_file: Path) -> None:
    async with client(token_file, holder.api, control_target=holder.api) as service:
        async with runner_on(holder) as runner:
            creating = asyncio.create_task(service.runner(DESTINATION).create(session_id=uuid4(), spec=SPEC))
            await runner.next(runner_control_pb2.OpenSessionRequest)
        with pytest.raises(ServiceError) as lost:
            await creating
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE


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
        runner_on(holder) as runner,
        client(token_file, other.api, control_target=other.api) as unrouted,
        client(token_file, other.api, control_target=holder.api) as routed,
    ):
        # Only the holder seals: a replica without the connection refuses, and the Sandbox Service's
        # call through the control Service reaches the holder.
        with pytest.raises(ServiceError) as refused:
            await sandbox_service_teardown(cluster, unrouted).advance(sealing)
        assert refused.value.code == grpc.StatusCode.UNAVAILABLE
        advancing = asyncio.create_task(sandbox_service_teardown(cluster, routed).advance(sealing))
        sealed = await runner.next(runner_pb2.SealRequest)
        response = runner_pb2.SealResponse()
        response.sessions.add(session_id=str(public_id), cursor=3)
        sealed.outcome.set_result(response)
        # The holder's answer reached the Sandbox Service, which waits for the ingress.
        with pytest.raises(SealNotFlushedError):
            await advancing
    assert (await cluster.inventory.get(SANDBOX)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING


COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="test input"))


async def test_a_command_reaches_the_holder_and_returns_the_runner_admission(
    holder: Replica, other: Replica, public_id: UUID, token_file: Path
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.origin.source_id, admitted.origin.sequence = "test-runner-source", 1
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    # The caller's own target is the other replica; the control Service names the holder.
    async with runner_on(holder) as runner, client(token_file, other.api, control_target=holder.api) as service:
        submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
        delivered = await runner.next(runner_control_pb2.DeliverCommandRequest)
        assert (delivered.request.session_id, delivered.request.command) == (str(public_id), COMMAND)
        delivered.outcome.set_result(runner_control_pb2.DeliverCommandResponse(admission=admitted))
        assert await submitting == admitted


async def test_a_refused_command_carries_the_runners_status(holder: Replica, public_id: UUID, token_file: Path) -> None:
    async with runner_on(holder) as runner, client(token_file, holder.api, control_target=holder.api) as service:
        submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
        # The command ID was admitted for other work.
        (await runner.next(runner_control_pb2.DeliverCommandRequest)).outcome.set_result(grpc.StatusCode.ALREADY_EXISTS)
        with pytest.raises(ServiceError) as refused:
            await submitting
        assert refused.value.code == grpc.StatusCode.ALREADY_EXISTS


async def test_a_lost_connection_fails_its_command_and_a_retry_delivers_it_again(
    holder: Replica, public_id: UUID, token_file: Path
) -> None:
    async with client(token_file, holder.api, control_target=holder.api) as service:
        async with runner_on(holder) as runner:
            submitting = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
            await runner.next(runner_control_pb2.DeliverCommandRequest)
        with pytest.raises(ServiceError) as lost:
            await submitting
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE
        async with runner_on(holder) as runner:
            retrying = asyncio.create_task(service.command(DESTINATION, str(public_id), COMMAND))
            assert (await runner.next(runner_control_pb2.DeliverCommandRequest)).request.command == COMMAND
            retrying.cancel()


if __name__ == "__main__":
    pytest_bazel.main()
