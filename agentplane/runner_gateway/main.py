"""Standalone Runner Gateway entry point: Sessions and commands, and the runner control stream listener."""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.grpc_process import background, kubernetes_configuration, until_terminated
from agentplane.grpc_serving import add_health, serving
from agentplane.proto import runner_control_pb2_grpc
from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import SubmissionStore
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases, ConnectionRecords
from agentplane.runner_gateway.grpc_api import Resources, add_service
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming, RunnerControlService
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.settings import Settings
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg
# The control servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//protobuf


def database_engine(url: str) -> AsyncEngine:
    return create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2)


@asynccontextmanager
async def gateway(
    settings: Settings, api: k8s_client.ApiClient, server: grpc.aio.Server, commands_engine: AsyncEngine
) -> AsyncIterator[Resources]:
    """Serve `RunnerGateway` on `server`, and runner control stream connections on their own listener."""
    core = k8s_client.CoreV1Api(api)
    inventory = SandboxInventory(
        namespace=settings.sandbox_namespace,
        core_v1=core,
        custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api)),
    )
    sessions = SessionRegistry(commands_engine)
    submissions = SubmissionStore(commands_engine)
    connections = ConnectionStore(commands_engine)
    destinations = DestinationResolver(inventory, core, settings.runner_port)
    leases = ConnectionLeases()
    timing = ControlTiming()
    control = LifecycleControl(leases)
    delivery = CommandDelivery(
        commands_engine.url,
        dispatches=DispatchStore(commands_engine),
        connections=connections,
        leases=leases,
        liveness_s=timing.liveness_s,
    )
    resources = Resources(
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces={
                a.namespace for a in (*settings.caller_accounts, *settings.sealer_accounts)
            },
        ),
        sessions=sessions,
        submissions=submissions,
        delivery=delivery,
        control=control,
        destinations=destinations,
        admission_timeout_s=settings.admission_timeout_s,
        runner_admission_ack_timeout_s=settings.runner_admission_ack_timeout_s,
        caller_accounts=settings.caller_accounts,
        sealer_accounts=settings.sealer_accounts,
        platform_instructions=settings.platform_instructions,
        lifecycle_timeout_s=settings.lifecycle_timeout_s,
        runner_grpc_channel_options=settings.runner_grpc_channel_options,
    )
    add_service(resources, server)
    if settings.port == settings.runner_control_port:
        raise ValueError("gRPC and runner control stream ports must differ")
    # A separate listener, so the network fence can admit runners to the control listener and nothing else.
    control_server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(
        RunnerControlService(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(api),
                audience=settings.runner_control_audience,
                allowed_service_account_namespaces={settings.sandbox_namespace},
            ),
            inventory=inventory,
            core=core,
            connections=connections,
            records=ConnectionRecords(
                api,
                namespace=settings.sandbox_namespace,
                holder=ConnectionHolder(pod_name=socket.gethostname(), pod_ip=settings.pod_ip, port=settings.port),
            ),
            delivery=delivery,
            control=control,
            timing=timing,
        ),
        control_server,
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(delivery.listener.listen())
        await stack.enter_async_context(
            serving(control_server, f"{settings.host}:{settings.runner_control_port}", grace_s=5)
        )
        await stack.enter_async_context(
            background(
                leases.watch(k8s_client.CoordinationV1Api(api), settings.sandbox_namespace, resync_seconds=300).run(),
                name="runner-connection-leases",
            )
        )
        yield resources


async def serve(settings: Settings) -> None:
    commands_engine = database_engine(settings.commands_database_url)
    try:
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
            AsyncExitStack() as stack,
        ):
            server = grpc.aio.server()
            add_health(server)
            await stack.enter_async_context(gateway(settings, api, server, commands_engine))
            await stack.enter_async_context(serving(server, f"{settings.host}:{settings.port}", grace_s=5))
            await until_terminated()
    finally:
        await commands_engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
