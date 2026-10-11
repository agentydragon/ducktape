"""The control listener against a fake API server and a migrated commands database."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_control_pb2_grpc
from agentplane.runner_gateway.command_delivery import CommandDelivery, CommandRefusedError
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import CommandSubmission, SubmissionStore
from agentplane.runner_gateway.connection_records import (
    CONNECTION_EPOCH_ANNOTATION,
    CONNECTION_LABEL,
    POD_UID_ANNOTATION,
    RUNNER_BOOT_ID_ANNOTATION,
    ConnectionHolder,
    ConnectionLeases,
    RecordOwner,
)
from agentplane.runner_gateway.destinations import DestinationUnavailableError
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.runner_control import OTHER_REPLICA, REPLICA, RUNNER_TOKEN, serve_replica_control
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.agent_sandbox import SANDBOX_KIND, SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="hello"))
RUNNER_SESSION = "test-runner-session"
HELLO = runner_control_pb2.RunnerFrame(hello=runner_control_pb2.Hello(runner_boot_id="test-boot"))
OWNER = RecordOwner(name=SANDBOX, uid=UUID(SANDBOX_UID))

type Replica = Callable[[ConnectionHolder], AbstractAsyncContextManager[runner_control_pb2_grpc.RunnerControlAsyncStub]]


@pytest.fixture
async def delivery(engine: AsyncEngine, leases: ConnectionLeases) -> AsyncIterator[CommandDelivery]:
    delivery = CommandDelivery(engine.url, dispatches=DispatchStore(engine), leases=leases, liveness_s=1, recheck_s=0.1)
    async with delivery.listener.listen():
        yield delivery


@pytest.fixture
def replica(cluster: Cluster, engine: AsyncEngine, delivery: CommandDelivery) -> Replica:
    """Start a channel listener as one gateway replica; replicas share the cluster and database."""
    return lambda holder: serve_replica_control(
        cluster,
        holder,
        delivery=delivery,
        control=LifecycleControl(delivery.leases),
        timing=ControlTiming(heartbeat_s=0.05, liveness_s=1, touch_s=0.1, max_age_s=60),
    )


@pytest.fixture
async def listener(replica: Replica) -> AsyncIterator[runner_control_pb2_grpc.RunnerControlAsyncStub]:
    async with replica(REPLICA) as stub:
        yield stub


async def until_cached(leases: ConnectionLeases, condition: Callable[[], bool]) -> None:
    """Wait for the Lease cache to catch up with what the API server holds."""
    async with asyncio.timeout(5):
        await leases.wait_for(condition)


def run_sandbox_as(cluster: Cluster, account: str) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["podTemplate"]["spec"]["serviceAccountName"] = account


@asynccontextmanager
async def connect(
    stub: runner_control_pb2_grpc.RunnerControlAsyncStub, token: str = RUNNER_TOKEN
) -> AsyncIterator[grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame]]:
    call = stub.Connect(metadata=(("authorization", f"Bearer {token}"),), timeout=10)
    try:
        yield call
    finally:
        call.cancel()


async def read_frame(
    call: grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame],
) -> runner_control_pb2.ServiceFrame:
    frame = await call.read()
    assert isinstance(frame, runner_control_pb2.ServiceFrame), "the service ended the stream"
    return frame


async def status_of(
    call: grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame],
) -> grpc.StatusCode:
    """Drain the service's frames until it ends the stream."""
    try:
        while (await call.read()) is not grpc.aio.EOF:
            pass
    except grpc.aio.AioRpcError as error:
        return error.code()
    return await call.code()


async def test_newest_connection_supersedes_the_previous_one(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub,
) -> None:
    async with connect(listener) as first:
        await first.write(HELLO)
        welcome = await read_frame(first)
        assert welcome.welcome == runner_control_pb2.Welcome(connection_epoch=1)
        assert (await read_frame(first)).HasField("heartbeat")
        async with connect(listener) as second:
            await second.write(HELLO)
            assert (await read_frame(second)).welcome.connection_epoch == 2
            assert await status_of(first) == grpc.StatusCode.ABORTED
            await second.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
            assert (await read_frame(second)).HasField("heartbeat")


def owners(obj: dict[str, Any]) -> list[tuple[str, str]]:
    return [(ref["kind"], ref["uid"]) for ref in obj["metadata"]["ownerReferences"]]


async def test_accepting_records_the_connection_on_the_sandbox(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub, cluster: Cluster
) -> None:
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).welcome.connection_epoch == 1
    lease = cluster.fake.objects["leases"][OWNER.lease_name]
    service = cluster.fake.objects["services"][OWNER.control_name]
    endpoints = cluster.fake.objects["endpointslices"][OWNER.control_name]
    # Deleting the Sandbox deletes all three.
    for record in (lease, service, endpoints):
        assert owners(record) == [(SANDBOX_KIND, SANDBOX_UID)]
    assert (lease["spec"]["holderIdentity"], lease["spec"]["leaseTransitions"]) == (REPLICA.pod_name, 1)
    assert lease["metadata"]["annotations"] == {
        POD_UID_ANNOTATION: "test-pod-uid",
        RUNNER_BOOT_ID_ANNOTATION: "test-boot",
    }
    assert service["spec"]["clusterIP"] == "None"
    assert "selector" not in service["spec"]
    assert endpoints["metadata"]["labels"]["kubernetes.io/service-name"] == service["metadata"]["name"]
    assert endpoints["metadata"]["annotations"][CONNECTION_EPOCH_ANNOTATION] == "1"
    assert endpoints["endpoints"][0]["addresses"] == [REPLICA.pod_ip]
    assert [port["name"] for port in endpoints["ports"]] == [port["name"] for port in service["spec"]["ports"]]


async def test_a_connection_through_another_replica_supersedes_this_one(replica: Replica, cluster: Cluster) -> None:
    async with replica(REPLICA) as this, replica(OTHER_REPLICA) as other, connect(this) as first:
        await first.write(HELLO)
        assert (await read_frame(first)).welcome.connection_epoch == 1
        async with connect(other) as second:
            await second.write(HELLO)
            assert (await read_frame(second)).welcome.connection_epoch == 2
            # The first replica learns it on its next renewal.
            assert await status_of(first) == grpc.StatusCode.ABORTED
    lease = cluster.fake.objects["leases"][OWNER.lease_name]
    assert (lease["spec"]["holderIdentity"], lease["spec"]["leaseTransitions"]) == (OTHER_REPLICA.pod_name, 2)
    endpoints = cluster.fake.objects["endpointslices"][OWNER.control_name]
    assert endpoints["metadata"]["annotations"][CONNECTION_EPOCH_ANNOTATION] == "2"
    assert endpoints["endpoints"][0]["addresses"] == [OTHER_REPLICA.pod_ip]


def owned_by_sandbox(name: str, **fields: Any) -> dict[str, Any]:
    owner = OWNER.reference()
    reference = {"apiVersion": owner.api_version, "kind": owner.kind, "name": owner.name, "uid": owner.uid}
    return {"name": name, "namespace": SANDBOX_NAMESPACE, "ownerReferences": [reference], **fields}


async def test_an_older_epoch_never_overwrites_the_control_endpoints(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub, cluster: Cluster
) -> None:
    # Written by a newer connection's owner that won the race to the EndpointSlice.
    newer: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": owned_by_sandbox(
            OWNER.control_name,
            labels={"kubernetes.io/service-name": OWNER.control_name},
            annotations={CONNECTION_EPOCH_ANNOTATION: "7"},
        ),
        "addressType": "IPv4",
        "endpoints": [{"addresses": ["192.0.2.99"]}],
        "ports": [{"name": "grpc", "port": REPLICA.port}],
    }
    cluster.fake.put("endpointslices", newer)
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).welcome.connection_epoch == 1
    endpoints = cluster.fake.objects["endpointslices"][OWNER.control_name]
    assert (endpoints["metadata"]["annotations"], endpoints["endpoints"]) == (
        newer["metadata"]["annotations"],
        newer["endpoints"],
    )


def connection_lease(*, epoch: int, renewed_at: datetime) -> dict[str, Any]:
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": owned_by_sandbox(OWNER.lease_name, labels={CONNECTION_LABEL: "true"}),
        "spec": {
            "holderIdentity": OTHER_REPLICA.pod_name,
            "leaseTransitions": epoch,
            "renewTime": f"{renewed_at:%Y-%m-%dT%H:%M:%S.%fZ}",
            "leaseDurationSeconds": 30,
        },
    }


async def test_liveness_is_read_from_the_watched_leases(leases: ConnectionLeases, cluster: Cluster) -> None:
    sandbox_uid = UUID(SANDBOX_UID)
    cluster.fake.put("leases", connection_lease(epoch=3, renewed_at=datetime.now(UTC)))
    await until_cached(leases, lambda: leases.read_live_epoch(sandbox_uid, liveness_s=30) == 3)
    # A connection that stopped renewing is no longer live, but stays the current one.
    cluster.fake.put("leases", connection_lease(epoch=4, renewed_at=datetime.now(UTC) - timedelta(seconds=31)))
    await until_cached(leases, lambda: leases.read_epoch(sandbox_uid) == 4)
    assert leases.read_live_epoch(sandbox_uid, liveness_s=30) is None
    cluster.fake.delete("leases", OWNER.lease_name)
    await until_cached(leases, lambda: leases.read_epoch(sandbox_uid) is None)


@pytest.mark.parametrize(
    ("token", "account", "code"),
    [
        ("test-unknown-token", ACCOUNT, grpc.StatusCode.UNAUTHENTICATED),
        (RUNNER_TOKEN, "test-other-account", grpc.StatusCode.PERMISSION_DENIED),
    ],
)
async def test_only_the_sandbox_pod_connects(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub,
    cluster: Cluster,
    token: str,
    account: str,
    code: grpc.StatusCode,
) -> None:
    run_sandbox_as(cluster, account)
    async with connect(listener, token) as call:
        await call.write(HELLO)
        assert await status_of(call) == code


async def test_first_frame_must_be_hello(listener: runner_control_pb2_grpc.RunnerControlAsyncStub) -> None:
    async with connect(listener) as call:
        await call.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
        assert await status_of(call) == grpc.StatusCode.INVALID_ARGUMENT


async def test_silent_runner_is_dropped(listener: runner_control_pb2_grpc.RunnerControlAsyncStub) -> None:
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        assert await status_of(call) == grpc.StatusCode.UNAVAILABLE


async def test_identity_change_ends_the_connection(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub, cluster: Cluster
) -> None:
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        run_sandbox_as(cluster, "test-other-account")
        assert await status_of(call) == grpc.StatusCode.PERMISSION_DENIED


@pytest.fixture
async def session_id(engine: AsyncEngine) -> UUID:
    session_id = uuid4()
    await SubmissionStore(engine).accept(
        session_id, CommandSubmission(command=COMMAND), caller=ServiceAccountRef(namespace="testing", name="caller")
    )
    return session_id


def deliver(delivery: CommandDelivery, cluster: Cluster, session_id: UUID) -> asyncio.Task[event_log_pb2.EventEntry]:
    return asyncio.create_task(
        delivery.deliver(
            sandbox_uid=UUID(SANDBOX_UID),
            session_id=session_id,
            command_id=COMMAND.command_id,
            runner_session_id=RUNNER_SESSION,
            deadline_s=10,
        )
    )


async def next_delivery(
    call: grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame],
) -> runner_control_pb2.DeliverCommand:
    while (frame := await read_frame(call)).HasField("heartbeat"):
        await call.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))
    return frame.deliver


async def test_delivered_command_returns_the_runner_receipt(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub,
    delivery: CommandDelivery,
    cluster: Cluster,
    session_id: UUID,
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.origin.source_id, admitted.origin.sequence = "test-runner-source", 1
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        await until_cached(
            delivery.leases, lambda: delivery.leases.read_live_epoch(UUID(SANDBOX_UID), liveness_s=1) is not None
        )
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        assert (sent.session_id, sent.command) == (RUNNER_SESSION, COMMAND)
        await call.write(
            runner_control_pb2.RunnerFrame(
                receipt=runner_control_pb2.CommandReceipt(
                    session_id=RUNNER_SESSION, command_id=sent.command.command_id, admitted=admitted
                )
            )
        )
        assert await asyncio.wait_for(waiting, timeout=10) == admitted


async def test_refusal_reaches_the_caller(
    listener: runner_control_pb2_grpc.RunnerControlAsyncStub,
    delivery: CommandDelivery,
    cluster: Cluster,
    session_id: UUID,
) -> None:
    refused = runner_control_pb2.CommandRefused(reason=runner_control_pb2.CommandRefused.REASON_HARNESS_STOPPED)
    async with connect(listener) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        await until_cached(
            delivery.leases, lambda: delivery.leases.read_live_epoch(UUID(SANDBOX_UID), liveness_s=1) is not None
        )
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        await call.write(
            runner_control_pb2.RunnerFrame(
                receipt=runner_control_pb2.CommandReceipt(
                    session_id=RUNNER_SESSION, command_id=sent.command.command_id, refused=refused
                )
            )
        )
        with pytest.raises(CommandRefusedError) as raised:
            await asyncio.wait_for(waiting, timeout=10)
        assert raised.value.refusal == refused


async def test_unconnected_runner_is_unavailable(delivery: CommandDelivery, cluster: Cluster, session_id: UUID) -> None:
    with pytest.raises(DestinationUnavailableError):
        await deliver(delivery, cluster, session_id)


if __name__ == "__main__":
    pytest_bazel.main()
