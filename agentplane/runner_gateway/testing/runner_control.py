"""One replica's runner control listener over the fake cluster, trusting the Sandbox Pod's runner."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.proto import runner_control_pb2_grpc
from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionRecords
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.control_listener import serve_runner_control
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# The control servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//protobuf

RUNNER_AUDIENCE = "test-runner-control"
RUNNER_TOKEN = "test-runner-token"
REPLICA = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER_REPLICA = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)


@asynccontextmanager
async def serve_channel(
    cluster: Cluster,
    holder: ConnectionHolder,
    *,
    delivery: CommandDelivery,
    control: LifecycleControl,
    timing: ControlTiming,
) -> AsyncIterator[runner_control_pb2_grpc.RunnerControlAsyncStub]:
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )
    async with (
        serve_runner_control(
            principals=cluster.build_principal_resolver(RUNNER_AUDIENCE),
            inventory=cluster.inventory,
            core=k8s_client.CoreV1Api(cluster.api),
            records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
            delivery=delivery,
            control=control,
            timing=timing,
        ) as port,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        yield runner_control_pb2_grpc.RunnerControlStub(channel)
