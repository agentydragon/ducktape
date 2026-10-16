"""Lifecycle calls and commands to the runner's `Runner` service over its control stream: a scripted
runner on one replica's channel listener, the public API on that replica and another, a fake API server
and a migrated database."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc.calls import RefusedError, ServiceError
from agentplane.grpc.tunnel import TunnelCaller, relay
from agentplane.proto import grpc_tunnel_pb2, runner_control_pb2, runner_pb2
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.auth import CALLER_TOKEN
from agentplane.runner_gateway.testing.grpc_service import service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.runner_control import (
    OTHER_REPLICA,
    REPLICA,
    ScriptedRunner,
    attached,
    connected,
    serve_replica_control,
)
from agentplane.sandboxes.testing.kubernetes import DESTINATION, SANDBOX_UID, SandboxCluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# gazelle:include_dep @pypi//protobuf

CALLER_AUDIENCE = "test-runner-gateway-lifecycle"
CALLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-caller")
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace/{session_id}", model="test-model")


@dataclass
class Replica:
    """One gateway replica: its channel listener's target, its public API and its runner connections."""

    listener: str
    api: str
    control: LifecycleControl


@pytest.fixture
def replica(
    cluster: SandboxCluster, leases: ConnectionLeases
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
                principals=cluster.build_principal_resolver(CALLER_AUDIENCE),
                destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
                caller_accounts=frozenset({CALLER}),
            ),
            control=control,
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
def public_id() -> UUID:
    """A Session identifier returned by the scripted Runner."""
    return uuid4()


@asynccontextmanager
async def runner_on(replica: Replica) -> AsyncIterator[ScriptedRunner]:
    """A scripted runner holding the Sandbox's connection to `replica`."""
    runner = ScriptedRunner()
    async with connected(replica.listener, runner, replica.control):
        yield runner


@asynccontextmanager
async def client(token_file: Path, control_target: str) -> AsyncIterator[SandboxControlClient]:
    yield SandboxControlClient(
        control_target=control_target, token_file=token_file, request_timeout_s=20, lifecycle_timeout_s=310
    )


async def test_tunnel_send_preserves_grpc_status_and_detail() -> None:
    upstream = grpc.aio.AioRpcError(
        grpc.StatusCode.INTERNAL, grpc.aio.Metadata(), grpc.aio.Metadata(), "control stream write failed"
    )

    async def fail_send(_call: object) -> None:
        raise upstream

    async with TunnelCaller(fail_send) as tunnel:
        with pytest.raises(grpc.aio.AioRpcError) as failed:
            await tunnel.forward("/agentplane.proto.Runner/ResumeSession", b"request", deadline_s=None)

    assert failed.value.code() == grpc.StatusCode.INTERNAL
    assert failed.value.details() == "control stream write failed"


async def test_tunnel_end_marks_unanswered_call_as_outcome_unknown() -> None:
    sent = asyncio.Event()

    async def send(_call: object) -> None:
        sent.set()

    async with TunnelCaller(send) as tunnel:
        call = asyncio.create_task(
            tunnel.forward("/agentplane.proto.Runner/ResumeSession", b"request", deadline_s=None)
        )
        await sent.wait()

    with pytest.raises(grpc.aio.AioRpcError) as failed:
        await call

    assert failed.value.code() == grpc.StatusCode.UNAVAILABLE
    assert failed.value.details() == "the tunnel ended before the callee answered"


async def test_tunnel_preserves_upstream_status_metadata() -> None:
    metadata = grpc.aio.Metadata(("x-runner-detail", "preserved"), ("x-runner-bin", bytes((0, 255))))
    upstream = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), metadata, "runner unavailable")

    class FailingRunnerCall:
        async def __call__(self, *_args: object, **_kwargs: object) -> bytes:
            raise upstream

    class FailingRunnerChannel:
        def unary_unary(self, _method: str) -> FailingRunnerCall:
            return FailingRunnerCall()

    tunnel: TunnelCaller

    async def send(call: grpc_tunnel_pb2.Call) -> None:
        reply = await relay(call, cast(grpc.aio.Channel, FailingRunnerChannel()))
        tunnel.answer(reply)

    async with TunnelCaller(send) as tunnel:
        with pytest.raises(grpc.aio.AioRpcError) as failed:
            await tunnel.forward("/agentplane.proto.Runner/ResumeSession", b"request", deadline_s=None)

    assert failed.value.code() == grpc.StatusCode.UNAVAILABLE
    assert failed.value.details() == "runner unavailable"
    assert tuple(failed.value.trailing_metadata() or ()) == tuple(metadata)


async def test_runner_open_retry_after_setup_failure_returns_the_attached_session(
    holder: Replica, token_file: Path
) -> None:
    # An identical Open retry reaches the replica that holds the Sandbox connection.
    async with runner_on(holder) as runner, client(token_file, holder.api) as service:
        sandbox = service.runner(DESTINATION)
        requested = uuid4()
        first = asyncio.create_task(sandbox.open_session(session_id=requested, spec=SPEC))
        opened = await runner.next(runner_control_pb2.OpenSessionRequest)
        session_id = opened.request.session_id
        # A harness that failed to launch.
        opened.outcome.set_result(grpc.StatusCode.FAILED_PRECONDITION)
        with pytest.raises(RefusedError):
            await first
        # The runner created the Session and could not start it; asked again it confirms it and
        # starts its harness.
        retried = asyncio.create_task(sandbox.open_session(session_id=requested, spec=SPEC))
        confirmed = await runner.next(runner_control_pb2.OpenSessionRequest)
        assert (confirmed.request.session_id, confirmed.request.spec) == (session_id, opened.request.spec)
        confirmed.outcome.set_result(
            runner_control_pb2.OpenSessionResponse(attached=attached(session_id, runner_pb2.HARNESS_STATE_RUNNING))
        )
        response = await retried
    assert response.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert response.session_id == session_id == str(requested)


async def test_runner_open_on_a_running_session_returns_without_resuming(holder: Replica, token_file: Path) -> None:
    async with runner_on(holder) as runner, client(token_file, holder.api) as service:
        creating = asyncio.create_task(service.runner(DESTINATION).open_session(session_id=uuid4(), spec=SPEC))
        await runner.open()
        # Waiting on a resume would never return.
        assert (await creating).harness_state == runner_pb2.HARNESS_STATE_RUNNING


async def test_listing_and_resuming_go_over_the_channel(holder: Replica, public_id: UUID, token_file: Path) -> None:
    async with runner_on(holder) as runner, client(token_file, holder.api) as service:
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
    async with runner_on(holder) as runner, client(token_file, other.api) as caller:
        with pytest.raises(ServiceError) as refused:
            await caller.runner(DESTINATION).open_session(session_id=uuid4(), spec=SPEC)
        assert refused.value.code == grpc.StatusCode.UNAVAILABLE
    assert not runner.received


async def test_a_superseded_connection_calls_nothing(holder: Replica, other: Replica, leases: ConnectionLeases) -> None:
    async with runner_on(holder), runner_on(other):
        async with asyncio.timeout(5):
            await leases.wait_for(lambda: leases.read_epoch(UUID(SANDBOX_UID)) == 2)
        with pytest.raises(ConnectionNotHeldError):
            holder.control.runner(UUID(SANDBOX_UID))


async def test_a_lost_connection_fails_its_call(holder: Replica, token_file: Path) -> None:
    async with client(token_file, holder.api) as service:
        async with runner_on(holder) as runner:
            creating = asyncio.create_task(service.runner(DESTINATION).open_session(session_id=uuid4(), spec=SPEC))
            await runner.next(runner_control_pb2.OpenSessionRequest)
        with pytest.raises(ServiceError) as lost:
            await creating
        assert lost.value.code == grpc.StatusCode.UNAVAILABLE
        assert str(lost.value) == "UNAVAILABLE: the tunnel ended before the callee answered"


if __name__ == "__main__":
    pytest_bazel.main()
