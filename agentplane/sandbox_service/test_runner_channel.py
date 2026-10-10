"""The channel listener against a fake API server and a migrated commands database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner_channel import protocol_pb2, protocol_pb2_grpc
from agentplane.sandbox_service.commands.connections import ConnectionStore
from agentplane.sandbox_service.kubernetes_views import RUNNER_CHANNEL_ANNOTATION
from agentplane.sandbox_service.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/commands:conftest

pytest_plugins = ("agentplane.sandbox_service.commands.conftest",)

AUDIENCE = "test-runner-channel"
TOKEN = "test-runner-token"
HELLO = protocol_pb2.RunnerFrame(
    hello=protocol_pb2.Hello(runner_boot_id="test-boot", capabilities=["commands", "test-future-capability"])
)


@pytest.fixture
async def channel(cluster: Cluster, engine: AsyncEngine) -> AsyncIterator[protocol_pb2_grpc.RunnerChannelAsyncStub]:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(AUDIENCE,),
    )
    route(cluster, "commands")
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


def route(cluster: Cluster, operations: str | None) -> None:
    metadata = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"]
    annotations = metadata.setdefault("annotations", {})
    if operations is None:
        annotations.pop(RUNNER_CHANNEL_ANNOTATION, None)
    else:
        annotations[RUNNER_CHANNEL_ANNOTATION] = operations


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
    ("token", "operations", "code"),
    [
        ("test-unknown-token", "commands", grpc.StatusCode.UNAUTHENTICATED),
        (TOKEN, None, grpc.StatusCode.PERMISSION_DENIED),
        (TOKEN, "test-other-operation", grpc.StatusCode.PERMISSION_DENIED),
    ],
)
async def test_only_a_channel_routed_sandbox_pod_connects(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub,
    cluster: Cluster,
    token: str,
    operations: str | None,
    code: grpc.StatusCode,
) -> None:
    route(cluster, operations)
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


async def test_routing_change_ends_the_connection(
    channel: protocol_pb2_grpc.RunnerChannelAsyncStub, cluster: Cluster
) -> None:
    async with connect(channel) as call:
        await call.write(HELLO)
        assert (await read_frame(call)).HasField("welcome")
        route(cluster, None)
        assert await status_of(call) == grpc.StatusCode.PERMISSION_DENIED


if __name__ == "__main__":
    pytest_bazel.main()
