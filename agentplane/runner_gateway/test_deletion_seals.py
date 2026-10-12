"""A deleting Sandbox of Agentplane's own kind is sealed by the replica holding its runner's
connection, against the fake API server and a scripted runner."""

from collections.abc import AsyncIterator
from typing import cast

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.proto import runner_control_pb2, runner_pb2
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.deletion_seals import DeletionSeals
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.sealing import Sealing
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.runner_control import (
    HELLO,
    OWN_RUNNER_TOKEN,
    OWN_SANDBOX,
    REPLICA,
    add_own_sandbox,
    serve_replica_control,
)
from agentplane.sandbox_service.testing.kubernetes import OWN_SANDBOXES, Cluster
from agentplane.service_process import background
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf

type Connect = grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame]


@pytest.fixture
async def runner(cluster: Cluster, leases: ConnectionLeases, sessions: SessionRegistry) -> AsyncIterator[Connect]:
    """The own Sandbox's runner, connected to a replica that runs the deletion seals."""
    add_own_sandbox(cluster)
    control = LifecycleControl(leases)
    seals = DeletionSeals(
        cast(CustomObjectsClient, k8s_client.CustomObjectsApi(cluster.api)),
        namespace=SANDBOX_NAMESPACE,
        sealing=Sealing(sessions, control=control),
    )
    async with (
        serve_replica_control(
            cluster, REPLICA, control=control, timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1)
        ) as stub,
        background(seals.run(resync_seconds=60), name="deletion-seals"),
    ):
        call = stub.Connect(metadata=(("authorization", f"Bearer {OWN_RUNNER_TOKEN}"),), timeout=30)
        await call.write(HELLO)
        welcome = await call.read()
        assert isinstance(welcome, runner_control_pb2.ServiceFrame)
        assert welcome.HasField("welcome")
        try:
            yield call
        finally:
            call.cancel()


async def next_request(call: Connect) -> runner_control_pb2.ControlRequest:
    """Answer heartbeats until the service sends a lifecycle request."""
    while True:
        frame = await call.read()
        assert isinstance(frame, runner_control_pb2.ServiceFrame), "the service ended the stream"
        if frame.HasField("control"):
            return frame.control
        await call.write(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))


def mark_deleting(cluster: Cluster) -> None:
    stored = cluster.fake.objects[OWN_SANDBOXES][OWN_SANDBOX]
    cluster.fake.put(
        OWN_SANDBOXES, {**stored, "metadata": {**stored["metadata"], "deletionTimestamp": "2026-10-12T08:00:00Z"}}
    )


def sealed_condition(cluster: Cluster) -> dict[str, str] | None:
    conditions = cluster.fake.objects[OWN_SANDBOXES][OWN_SANDBOX].get("status", {}).get("conditions", [])
    return next((c for c in conditions if c["type"] == sandbox_resource.SESSIONS_SEALED), None)


async def test_a_deleting_sandbox_is_sealed_then_marked(runner: Connect, cluster: Cluster) -> None:
    mark_deleting(cluster)
    request = await next_request(runner)
    assert request.WhichOneof("verb") == "seal"
    # Not marked before the runner answers.
    assert sealed_condition(cluster) is None
    await runner.write(
        runner_control_pb2.RunnerFrame(
            control_result=runner_control_pb2.ControlResult(
                request_id=request.request_id, seal=runner_pb2.SealResponse()
            )
        )
    )
    await cluster.fake.wait_for(lambda: sealed_condition(cluster) is not None)
    condition = sealed_condition(cluster)
    assert condition is not None
    assert condition["status"] == "True"


if __name__ == "__main__":
    pytest_bazel.main()
