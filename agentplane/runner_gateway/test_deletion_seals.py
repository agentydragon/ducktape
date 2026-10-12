"""A deleting Sandbox is sealed by the replica holding its runner's
connection, against the fake API server and a scripted runner."""

from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.deletion_seals import DeletionSeals
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.sealing import Sealing
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.runner_control import REPLICA, ScriptedRunner, connected, serve_replica_control
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, Cluster
from agentplane.service_process import background
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
async def runner(
    cluster: Cluster, leases: ConnectionLeases, sessions: SessionRegistry
) -> AsyncIterator[ScriptedRunner]:
    """The Sandbox's runner, connected to a replica that runs the deletion seals."""
    control = LifecycleControl(leases)
    seals = DeletionSeals(
        cast(CustomObjectsClient, k8s_client.CustomObjectsApi(cluster.api)),
        namespace=SANDBOX_NAMESPACE,
        sealing=Sealing(sessions, control=control),
    )
    runner = ScriptedRunner()
    async with (
        serve_replica_control(
            cluster, REPLICA, control=control, timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1)
        ) as target,
        connected(target, runner, control),
        background(seals.run(resync_seconds=60), name="deletion-seals"),
    ):
        yield runner


def mark_deleting(cluster: Cluster) -> None:
    stored = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    cluster.fake.put(
        sandbox_resource.SANDBOXES_PLURAL,
        {**stored, "metadata": {**stored["metadata"], "deletionTimestamp": "2026-10-12T08:00:00Z"}},
    )


def sealed_condition(cluster: Cluster) -> dict[str, str] | None:
    conditions = (
        cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX].get("status", {}).get("conditions", [])
    )
    return next((c for c in conditions if c["type"] == sandbox_resource.SESSIONS_SEALED), None)


async def test_a_deleting_sandbox_is_sealed_then_marked(runner: ScriptedRunner, cluster: Cluster) -> None:
    mark_deleting(cluster)
    seal = await runner.next(runner_pb2.SealRequest)
    # Not marked before the runner answers.
    assert sealed_condition(cluster) is None
    seal.outcome.set_result(runner_pb2.SealResponse())
    await cluster.fake.wait_for(lambda: sealed_condition(cluster) is not None)
    condition = sealed_condition(cluster)
    assert condition is not None
    assert condition["status"] == "True"


if __name__ == "__main__":
    pytest_bazel.main()
