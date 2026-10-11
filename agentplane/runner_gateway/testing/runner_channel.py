"""One replica's runner channel listener over the fake cluster, trusting the Sandbox Pod's runner."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.proto import runner_channel_pb2_grpc
from agentplane.runner_gateway.channel_control import ChannelControl
from agentplane.runner_gateway.channel_delivery import ChannelDelivery
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionRecords
from agentplane.runner_gateway.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.testing.grpc_service import serving
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# The channel servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_channel_pb2
# gazelle:include_dep @pypi//protobuf

RUNNER_AUDIENCE = "test-runner-channel"
RUNNER_TOKEN = "test-runner-token"
REPLICA = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER_REPLICA = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)


@asynccontextmanager
async def serve_channel(
    cluster: Cluster,
    holder: ConnectionHolder,
    *,
    connections: ConnectionStore,
    delivery: ChannelDelivery,
    control: ChannelControl,
    timing: ChannelTiming,
) -> AsyncIterator[runner_channel_pb2_grpc.RunnerChannelAsyncStub]:
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )
    server = grpc.aio.server()
    runner_channel_pb2_grpc.add_RunnerChannelServicer_to_server(
        RunnerChannelService(
            principals=cluster.build_principal_resolver(RUNNER_AUDIENCE),
            inventory=cluster.inventory,
            core=k8s_client.CoreV1Api(cluster.api),
            connections=connections,
            records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
            delivery=delivery,
            control=control,
            timing=timing,
        ),
        server,
    )
    async with serving(server) as target, grpc.aio.insecure_channel(target) as channel:
        yield runner_channel_pb2_grpc.RunnerChannelStub(channel)
