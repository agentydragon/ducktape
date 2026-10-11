"""The channel listener against a fake API server and a migrated commands database."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import command_pb2, event_log_pb2, runner_channel_pb2, runner_channel_pb2_grpc
from agentplane.runner_gateway.channel_control import ChannelControl
from agentplane.runner_gateway.channel_delivery import ChannelDelivery, CommandRefusedError
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import CommandSubmission, SubmissionStore
from agentplane.runner_gateway.connection_records import (
    CONNECTION_EPOCH_ANNOTATION,
    CONNECTION_LABEL,
    POD_UID_ANNOTATION,
    RUNNER_BOOT_ID_ANNOTATION,
    ConnectionHolder,
    ConnectionLeases,
    ConnectionRecords,
    RecordOwner,
)
from agentplane.runner_gateway.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.destinations import DestinationUnavailableError
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOX_KIND, SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/runner_gateway/commands:conftest

pytest_plugins = ("agentplane.runner_gateway.commands.conftest",)

AUDIENCE = "test-runner-channel"
TOKEN = "test-runner-token"
COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="hello"))
RUNNER_SESSION = "test-runner-session"
HELLO = runner_channel_pb2.RunnerFrame(
    hello=runner_channel_pb2.Hello(runner_boot_id="test-boot", capabilities=["commands", "test-future-capability"])
)
REPLICA = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER_REPLICA = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)
OWNER = RecordOwner(name=SANDBOX, uid=UUID(SANDBOX_UID))

type Replica = Callable[[ConnectionHolder], AbstractAsyncContextManager[runner_channel_pb2_grpc.RunnerChannelAsyncStub]]


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
async def delivery(engine: AsyncEngine, leases: ConnectionLeases) -> AsyncIterator[ChannelDelivery]:
    delivery = ChannelDelivery(
        engine.url,
        dispatches=DispatchStore(engine),
        connections=ConnectionStore(engine),
        leases=leases,
        liveness_s=1,
        recheck_s=0.1,
    )
    async with delivery.listener.listen():
        yield delivery


@pytest.fixture
def replica(cluster: Cluster, engine: AsyncEngine, delivery: ChannelDelivery) -> Replica:
    """Start a channel listener as one Sandbox Service replica; replicas share the cluster and database."""
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(AUDIENCE,),
    )

    @asynccontextmanager
    async def start(holder: ConnectionHolder) -> AsyncIterator[runner_channel_pb2_grpc.RunnerChannelAsyncStub]:
        server = grpc.aio.server()
        runner_channel_pb2_grpc.add_RunnerChannelServicer_to_server(
            RunnerChannelService(
                principals=WorkloadPrincipalResolver(
                    authentication=k8s_client.AuthenticationV1Api(cluster.api),
                    audience=AUDIENCE,
                    allowed_service_account_namespaces={SANDBOX_NAMESPACE},
                ),
                inventory=cluster.inventory,
                core=k8s_client.CoreV1Api(cluster.api),
                connections=ConnectionStore(engine),
                records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
                delivery=delivery,
                control=ChannelControl(delivery.leases),
                timing=ChannelTiming(heartbeat_s=0.05, liveness_s=1, touch_s=0.1, max_age_s=60),
            ),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as connection:
                yield runner_channel_pb2_grpc.RunnerChannelStub(connection)
        finally:
            await server.stop(0)

    return start


@pytest.fixture
async def channel(replica: Replica) -> AsyncIterator[runner_channel_pb2_grpc.RunnerChannelAsyncStub]:
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
    stub: runner_channel_pb2_grpc.RunnerChannelAsyncStub, token: str = TOKEN
) -> AsyncIterator[grpc.aio.StreamStreamCall[runner_channel_pb2.RunnerFrame, runner_channel_pb2.ServiceFrame]]:
    call = stub.Connect(metadata=(("authorization", f"Bearer {token}"),), timeout=10)
    try:
        yield call
    finally:
        call.cancel()


async def read_frame(
    call: grpc.aio.StreamStreamCall[runner_channel_pb2.RunnerFrame, runner_channel_pb2.ServiceFrame],
) -> runner_channel_pb2.ServiceFrame:
    frame = await call.read()
    assert isinstance(frame, runner_channel_pb2.ServiceFrame), "the service ended the stream"
    return frame


async def status_of(
    call: grpc.aio.StreamStreamCall[runner_channel_pb2.RunnerFrame, runner_channel_pb2.ServiceFrame],
) -> grpc.StatusCode:
    """Drain the service's frames until it ends the stream."""
    try:
        while (await call.read()) is not grpc.aio.EOF:
            pass
    except grpc.aio.AioRpcError as error:
        return error.code()
    return await call.code()


async def test_newest_connection_supersedes_the_previous_one(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub,
) -> None:
    async with connect(channel) as first:
        await first.write(HELLO)
        welcome = await read_frame(first)
        assert welcome.welcome == runner_channel_pb2.Welcome(connection_epoch=1, capabilities=["commands"])
        assert (await read_frame(first)).HasField("heartbeat")
        async with connect(channel) as second:
            await second.write(HELLO)
            assert (await read_frame(second)).welcome.connection_epoch == 2
            assert await status_of(first) == grpc.StatusCode.ABORTED
            await second.write(runner_channel_pb2.RunnerFrame(heartbeat=runner_channel_pb2.Heartbeat()))
            assert (await read_frame(second)).HasField("heartbeat")


def owners(obj: dict[str, Any]) -> list[tuple[str, str]]:
    return [(ref["kind"], ref["uid"]) for ref in obj["metadata"]["ownerReferences"]]


async def test_accepting_records_the_connection_on_the_sandbox(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster
) -> None:
    async with connect(channel) as call:
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
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster
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
    async with connect(channel) as call:
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
        (TOKEN, "test-other-account", grpc.StatusCode.PERMISSION_DENIED),
    ],
)
async def test_only_the_sandbox_pod_connects(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub,
    cluster: Cluster,
    token: str,
    account: str,
    code: grpc.StatusCode,
) -> None:
    run_sandbox_as(cluster, account)
    async with connect(channel, token) as call:
        await call.write(HELLO)
        assert await status_of(call) == code


async def test_first_frame_must_be_hello(channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub) -> None:
    async with connect(channel) as call:
        await call.write(runner_channel_pb2.RunnerFrame(heartbeat=runner_channel_pb2.Heartbeat()))
        assert await status_of(call) == grpc.StatusCode.INVALID_ARGUMENT


async def test_silent_runner_is_dropped(channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub) -> None:
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        assert await status_of(call) == grpc.StatusCode.UNAVAILABLE


async def test_identity_change_ends_the_connection(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster
) -> None:
    async with connect(channel) as call:
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


def deliver(delivery: ChannelDelivery, cluster: Cluster, session_id: UUID) -> asyncio.Task[event_log_pb2.EventEntry]:
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
    call: grpc.aio.StreamStreamCall[runner_channel_pb2.RunnerFrame, runner_channel_pb2.ServiceFrame],
) -> runner_channel_pb2.DeliverCommand:
    while (frame := await read_frame(call)).HasField("heartbeat"):
        await call.write(runner_channel_pb2.RunnerFrame(heartbeat=runner_channel_pb2.Heartbeat()))
    return frame.deliver


async def test_delivered_command_returns_the_runner_receipt(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub,
    delivery: ChannelDelivery,
    cluster: Cluster,
    session_id: UUID,
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.origin.source_id, admitted.origin.sequence = "test-runner-source", 1
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        await until_cached(
            delivery.leases, lambda: delivery.leases.read_live_epoch(UUID(SANDBOX_UID), liveness_s=1) is not None
        )
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        assert (sent.session_id, sent.command) == (RUNNER_SESSION, COMMAND)
        await call.write(
            runner_channel_pb2.RunnerFrame(
                receipt=runner_channel_pb2.CommandReceipt(
                    session_id=RUNNER_SESSION, command_id=sent.command.command_id, admitted=admitted
                )
            )
        )
        assert await asyncio.wait_for(waiting, timeout=10) == admitted


async def test_refusal_reaches_the_caller(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub,
    delivery: ChannelDelivery,
    cluster: Cluster,
    session_id: UUID,
) -> None:
    refused = runner_channel_pb2.CommandRefused(reason=runner_channel_pb2.CommandRefused.REASON_HARNESS_STOPPED)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        await until_cached(
            delivery.leases, lambda: delivery.leases.read_live_epoch(UUID(SANDBOX_UID), liveness_s=1) is not None
        )
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        await call.write(
            runner_channel_pb2.RunnerFrame(
                receipt=runner_channel_pb2.CommandReceipt(
                    session_id=RUNNER_SESSION, command_id=sent.command.command_id, refused=refused
                )
            )
        )
        with pytest.raises(CommandRefusedError) as raised:
            await asyncio.wait_for(waiting, timeout=10)
        assert raised.value.refusal == refused


async def test_unconnected_runner_is_unavailable(delivery: ChannelDelivery, cluster: Cluster, session_id: UUID) -> None:
    with pytest.raises(DestinationUnavailableError):
        await deliver(delivery, cluster, session_id)


async def test_agreed_operations_stick_to_the_sandbox(
    channel: runner_channel_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster, engine: AsyncEngine
) -> None:
    sandbox_uid = UUID(SANDBOX_UID)
    connections = ConnectionStore(engine)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
    # A later runner that no longer offers commands gets none, but the Sandbox stays channel-routed.
    async with connect(channel) as call:
        await call.write(
            runner_channel_pb2.RunnerFrame(hello=runner_channel_pb2.Hello(runner_boot_id="test-later-boot"))
        )
        assert list((await read_frame(call)).welcome.capabilities) == []
    assert await connections.read_operations(sandbox_uid) == {"commands"}


if __name__ == "__main__":
    pytest_bazel.main()
