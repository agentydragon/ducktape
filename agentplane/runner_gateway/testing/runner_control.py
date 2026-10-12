"""One replica's runner control listener over the fake cluster, trusting the Sandbox Pod's runner."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.proto import runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionRecords
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.control_listener import serve_runner_control
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, OWN_SANDBOXES, SANDBOX, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.kubernetes import CustomObjectsClient

# The control servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//protobuf

RUNNER_AUDIENCE = "test-runner-control"
RUNNER_TOKEN = "test-runner-token"
REPLICA = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER_REPLICA = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)
HELLO = runner_control_pb2.RunnerFrame(hello=runner_control_pb2.Hello(runner_boot_id="test-boot"))
OWN_SANDBOX = "test-own-sandbox"
OWN_SANDBOX_UID = "7d6c1f0e-35b4-4c3e-9a51-2f8e0b9d6a10"
OWN_RUNNER_TOKEN = "test-own-runner-token"


def add_own_sandbox(cluster: Cluster) -> None:
    """One of Agentplane's own Sandboxes, its Pod running as the ServiceAccount of its name as the
    Sandbox Controller runs it, and `OWN_RUNNER_TOKEN` for that Pod's runner."""
    reference = {
        "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
        "kind": sandbox_resource.SANDBOX_KIND,
        "name": OWN_SANDBOX,
        "uid": OWN_SANDBOX_UID,
        "controller": True,
    }
    cluster.fake.put(
        OWN_SANDBOXES,
        {
            "apiVersion": reference["apiVersion"],
            "kind": reference["kind"],
            "metadata": {"name": OWN_SANDBOX, "uid": OWN_SANDBOX_UID},
            "spec": {"template": "test-template", "running": True},
        },
    )
    cluster.fake.pods[OWN_SANDBOX] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": OWN_SANDBOX,
            "namespace": SANDBOX_NAMESPACE,
            "uid": "test-own-pod-uid",
            "ownerReferences": [reference],
        },
        "spec": {
            "serviceAccountName": OWN_SANDBOX,
            "containers": [{"name": "runner", "image": "registry.test/unused"}],
        },
        "status": {"phase": "Running"},
    }
    cluster.fake.tokens[OWN_RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{OWN_SANDBOX}",
        pod_name=OWN_SANDBOX,
        pod_uid="test-own-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )


def attached(session_id: str, harness_state: runner_pb2.HarnessState) -> runner_pb2.Attached:
    """A runner's answer to an open or resume of a Session without setup."""
    return runner_pb2.Attached(
        session_id=session_id, harness_state=harness_state, setup_state=runner_pb2.SETUP_STATE_NOT_REQUIRED
    )


@asynccontextmanager
async def serve_replica_control(
    cluster: Cluster, holder: ConnectionHolder, *, control: LifecycleControl, timing: ControlTiming
) -> AsyncIterator[runner_control_pb2_grpc.RunnerControlAsyncStub]:
    """Serve `holder`'s control listener, and yield a stub that dials it as the Sandbox's runner."""
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )
    async with (
        serve_runner_control(
            principals=cluster.build_principal_resolver(RUNNER_AUDIENCE),
            sandboxes=cluster.inventory,
            custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(cluster.api)),
            core=k8s_client.CoreV1Api(cluster.api),
            records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
            control=control,
            timing=timing,
        ) as port,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        yield runner_control_pb2_grpc.RunnerControlStub(channel)
