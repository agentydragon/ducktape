"""A gateway's runner control listener on a loopback port, and an in-process runner dialing it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_process import background
from agentplane.grpc_serving import serving
from agentplane.proto import runner_control_pb2_grpc
from agentplane.runner import control as runner_control
from agentplane.runner.service import Runner
from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionRecords
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming, RunnerControlService
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The control servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//protobuf

# Frequent enough that a connection's Lease stays live under the tests' one-second delivery liveness.
GATEWAY_TIMING = ControlTiming(heartbeat_s=0.1, liveness_s=5, touch_s=0.2)
RUNNER_TIMING = runner_control.ControlTiming(heartbeat_s=0.1, liveness_s=5, backoff_initial_s=0.05, backoff_max_s=0.5)
HOLDER = ConnectionHolder(pod_name="test-gateway", pod_ip="192.0.2.10", port=18080)


@asynccontextmanager
async def serve_runner_control(
    *,
    principals: WorkloadPrincipalResolver,
    inventory: SandboxInventory,
    core: k8s_client.CoreV1Api,
    records: ConnectionRecords,
    delivery: CommandDelivery,
    control: LifecycleControl,
    timing: ControlTiming = GATEWAY_TIMING,
) -> AsyncIterator[int]:
    """Serve `RunnerControl` on a loopback port and yield the port."""
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(
        RunnerControlService(
            principals=principals,
            inventory=inventory,
            core=core,
            connections=delivery.connections,
            records=records,
            delivery=delivery,
            control=control,
            timing=timing,
        ),
        server,
    )
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield port


async def wait_connected(resources: Resources, sandbox_uid: UUID) -> None:
    """Until this replica holds the Sandbox's connection and its Lease reads live to command delivery.
    Renewals rewrite the Lease every `touch_s`, so the Lease watch re-checks after the holder registers."""
    delivery = resources.delivery
    async with asyncio.timeout(10):
        await delivery.leases.wait_for(
            lambda: (
                resources.control.holder(sandbox_uid) is not None
                and delivery.leases.read_live_epoch(sandbox_uid, liveness_s=delivery.liveness_s) is not None
            )
        )


@asynccontextmanager
async def control_plane(
    resources: Resources, *, principals: WorkloadPrincipalResolver, api: k8s_client.ApiClient
) -> AsyncIterator[str]:
    """What the gateway process runs beside `resources` for runners to dial: the control listener,
    its Postgres wakeups and its Lease watch, keeping connection records through `api`. Yields the
    listener's target."""
    inventory = resources.destinations.inventory
    async with (
        resources.delivery.listener.listen(),
        background(
            resources.delivery.leases.watch(
                k8s_client.CoordinationV1Api(api), inventory.namespace, resync_seconds=60
            ).run(),
            name="runner-connection-leases",
        ),
        serve_runner_control(
            principals=principals,
            inventory=inventory,
            core=resources.destinations.core,
            records=ConnectionRecords(api, namespace=inventory.namespace, holder=HOLDER),
            delivery=resources.delivery,
            control=resources.control,
        ) as port,
    ):
        yield f"127.0.0.1:{port}"


@asynccontextmanager
async def dialing(runner: Runner, target: str, bearer: str) -> AsyncIterator[None]:
    """`runner` keeps a control stream to `target`, as the runner process does with `--control-target`."""
    client = runner_control.RunnerControlClient(runner, target=target, bearer=bearer, timing=RUNNER_TIMING)
    async with background(client.run(), name="runner control stream"):
        yield
