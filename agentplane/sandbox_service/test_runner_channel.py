"""The channel listener against a fake API server and a migrated commands database."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner_channel import protocol_pb2, protocol_pb2_grpc
from agentplane.sandbox_service.channel_delivery import ChannelDelivery, CommandRefusedError
from agentplane.sandbox_service.commands.connections import ConnectionStore
from agentplane.sandbox_service.commands.dispatch import DispatchStore
from agentplane.sandbox_service.commands.store import CommandSubmission, SubmissionStore
from agentplane.sandbox_service.destinations import DestinationUnavailableError
from agentplane.sandbox_service.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/commands:conftest

pytest_plugins = ("agentplane.sandbox_service.commands.conftest",)

AUDIENCE = "test-runner-channel"
TOKEN = "test-runner-token"
COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="hello"))
RUNNER_SESSION = "test-runner-session"
HELLO = protocol_pb2.RunnerFrame(
    hello=protocol_pb2.Hello(runner_boot_id="test-boot", capabilities=["commands", "test-future-capability"])
)


@pytest.fixture
async def delivery(engine: AsyncEngine) -> AsyncIterator[ChannelDelivery]:
    delivery = ChannelDelivery(
        engine.url, dispatches=DispatchStore(engine), connections=ConnectionStore(engine), liveness_s=1, recheck_s=0.1
    )
    async with delivery.listener.listen():
        yield delivery


@pytest.fixture
async def channel(
    cluster: Cluster, engine: AsyncEngine, delivery: ChannelDelivery
) -> AsyncIterator[protocol_pb2_grpc.RunnerChannelAsyncStub]:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(AUDIENCE,),
    )
    server = grpc.aio.server()
    protocol_pb2_grpc.add_RunnerChannelServicer_to_server(
        RunnerChannelService(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(cluster.api),
                audience=AUDIENCE,
                allowed_service_account_namespaces={SANDBOX_NAMESPACE},
            ),
            inventory=cluster.inventory,
            core=k8s_client.CoreV1Api(cluster.api),
            connections=ConnectionStore(engine),
            delivery=delivery,
            replica="test-replica",
            timing=ChannelTiming(heartbeat_s=0.05, liveness_s=1, touch_s=0.1, max_age_s=60),
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as connection:
            yield protocol_pb2_grpc.RunnerChannelStub(connection)
    finally:
        await server.stop(0)


def run_sandbox_as(cluster: Cluster, account: str) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["spec"]["podTemplate"]["spec"]["serviceAccountName"] = account


@asynccontextmanager
async def connect(
    stub: protocol_pb2_grpc.RunnerChannelAsyncStub, token: str = TOKEN
) -> AsyncIterator[grpc.aio.StreamStreamCall[protocol_pb2.RunnerFrame, protocol_pb2.ServiceFrame]]:
    call = stub.Connect(metadata=(("authorization", f"Bearer {token}"),), timeout=10)
    try:
        yield call
    finally:
        call.cancel()


async def read_frame(
    call: grpc.aio.StreamStreamCall[protocol_pb2.RunnerFrame, protocol_pb2.ServiceFrame],
) -> protocol_pb2.ServiceFrame:
    frame = await call.read()
    assert isinstance(frame, protocol_pb2.ServiceFrame), "the service ended the stream"
    return frame


async def status_of(
    call: grpc.aio.StreamStreamCall[protocol_pb2.RunnerFrame, protocol_pb2.ServiceFrame],
) -> grpc.StatusCode:
    """Drain the service's frames until it ends the stream."""
    try:
        while (await call.read()) is not grpc.aio.EOF:
            pass
    except grpc.aio.AioRpcError as error:
        return error.code()
    return await call.code()


async def test_newest_connection_supersedes_the_previous_one(channel: protocol_pb2_grpc.RunnerChannelAsyncStub) -> None:
    async with connect(channel) as first:
        await first.write(HELLO)
        welcome = await read_frame(first)
        assert welcome.welcome == protocol_pb2.Welcome(connection_epoch=1, capabilities=["commands"])
        assert (await read_frame(first)).HasField("heartbeat")
        async with connect(channel) as second:
            await second.write(HELLO)
            assert (await read_frame(second)).welcome.connection_epoch == 2
            assert await status_of(first) == grpc.StatusCode.ABORTED
            await second.write(protocol_pb2.RunnerFrame(heartbeat=protocol_pb2.Heartbeat()))
            assert (await read_frame(second)).HasField("heartbeat")


@pytest.mark.parametrize(
    ("token", "account", "code"),
    [
        ("test-unknown-token", ACCOUNT, grpc.StatusCode.UNAUTHENTICATED),
        (TOKEN, "test-other-account", grpc.StatusCode.PERMISSION_DENIED),
    ],
)
async def test_only_the_sandbox_pod_connects(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster, token: str, account: str, code: grpc.StatusCode
) -> None:
    run_sandbox_as(cluster, account)
    async with connect(channel, token) as call:
        await call.write(HELLO)
        assert await status_of(call) == code


async def test_first_frame_must_be_hello(channel: protocol_pb2_grpc.RunnerChannelAsyncStub) -> None:
    async with connect(channel) as call:
        await call.write(protocol_pb2.RunnerFrame(heartbeat=protocol_pb2.Heartbeat()))
        assert await status_of(call) == grpc.StatusCode.INVALID_ARGUMENT


async def test_silent_runner_is_dropped(channel: protocol_pb2_grpc.RunnerChannelAsyncStub) -> None:
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        assert await status_of(call) == grpc.StatusCode.UNAVAILABLE


async def test_identity_change_ends_the_connection(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster
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
            sandbox_uid=UUID(cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"]["uid"]),
            session_id=session_id,
            command_id=COMMAND.command_id,
            runner_session_id=RUNNER_SESSION,
            deadline_s=10,
        )
    )


async def next_delivery(
    call: grpc.aio.StreamStreamCall[protocol_pb2.RunnerFrame, protocol_pb2.ServiceFrame],
) -> protocol_pb2.DeliverCommand:
    while (frame := await read_frame(call)).HasField("heartbeat"):
        await call.write(protocol_pb2.RunnerFrame(heartbeat=protocol_pb2.Heartbeat()))
    return frame.deliver


async def test_delivered_command_returns_the_runner_receipt(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, delivery: ChannelDelivery, cluster: Cluster, session_id: UUID
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.origin.source_id, admitted.origin.sequence = "test-runner-source", 1
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        assert (sent.session_id, sent.command) == (RUNNER_SESSION, COMMAND)
        await call.write(
            protocol_pb2.RunnerFrame(
                receipt=protocol_pb2.CommandReceipt(
                    session_id=RUNNER_SESSION, command_id=sent.command.command_id, admitted=admitted
                )
            )
        )
        assert await asyncio.wait_for(waiting, timeout=10) == admitted


async def test_refusal_reaches_the_caller(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, delivery: ChannelDelivery, cluster: Cluster, session_id: UUID
) -> None:
    refused = protocol_pb2.CommandRefused(reason=protocol_pb2.CommandRefused.REASON_HARNESS_STOPPED)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        waiting = deliver(delivery, cluster, session_id)
        sent = await next_delivery(call)
        await call.write(
            protocol_pb2.RunnerFrame(
                receipt=protocol_pb2.CommandReceipt(
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
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster, engine: AsyncEngine
) -> None:
    sandbox_uid = UUID(cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"]["uid"])
    connections = ConnectionStore(engine)
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
    # A later runner that no longer offers commands gets none, but the Sandbox stays channel-routed.
    async with connect(channel) as call:
        await call.write(protocol_pb2.RunnerFrame(hello=protocol_pb2.Hello(runner_boot_id="test-later-boot")))
        assert list((await read_frame(call)).welcome.capabilities) == []
    assert await connections.read_operations(sandbox_uid) == {"commands"}


if __name__ == "__main__":
    pytest_bazel.main()
