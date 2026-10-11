"""Standalone Runner Gateway entry point: Sessions and commands, and the runner channel listener."""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.proto import runner_channel_pb2_grpc
from agentplane.runner_gateway.channel_control import ChannelControl
from agentplane.runner_gateway.channel_delivery import ChannelDelivery
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import SubmissionStore
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases, ConnectionRecords
from agentplane.runner_gateway.grpc_api import Resources, add_service
from agentplane.runner_gateway.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.settings import Gateway, Settings
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.session_history.session_changes import SessionChanges
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg
# The channel servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_channel_pb2
# gazelle:include_dep @pypi//protobuf


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


def database_engine(url: str) -> AsyncEngine:
    return create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2)


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None], *, name: str) -> AsyncIterator[None]:
    task = asyncio.create_task(coroutine, name=name)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@asynccontextmanager
async def started(server: grpc.aio.Server) -> AsyncIterator[None]:
    await server.start()
    try:
        yield
    finally:
        await server.stop(grace=5)


async def serve_health(host: str, port: int) -> None:
    health = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @health.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    await uvicorn.Server(uvicorn.Config(health, host=host, port=port, access_log=False)).serve()


@asynccontextmanager
async def gateway(
    settings: Gateway,
    api: k8s_client.ApiClient,
    server: grpc.aio.Server,
    commands_engine: AsyncEngine,
    *,
    sealer_accounts: frozenset[ServiceAccountRef],
) -> AsyncIterator[Resources]:
    """Serve `RunnerGateway` on `server`, and runner channel connections on their own listener."""
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
    timing = ChannelTiming()
    control = ChannelControl(leases)
    delivery = ChannelDelivery(
        commands_engine.url,
        dispatches=DispatchStore(commands_engine),
        connections=connections,
        leases=leases,
        liveness_s=timing.liveness_s,
    )
    session_changes = SessionChanges(commands_engine.url)
    resources = Resources(
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces={a.namespace for a in (*settings.caller_accounts, *sealer_accounts)},
        ),
        sessions=sessions,
        submissions=submissions,
        delivery=delivery,
        control=control,
        session_changes=session_changes,
        destinations=destinations,
        admission_timeout_s=settings.admission_timeout_s,
        runner_admission_ack_timeout_s=settings.runner_admission_ack_timeout_s,
        follow_lease_s=settings.follow_lease_s,
        caller_accounts=settings.caller_accounts,
        sealer_accounts=sealer_accounts,
        platform_instructions=settings.platform_instructions,
        lifecycle_timeout_s=settings.lifecycle_timeout_s,
        runner_grpc_channel_options=settings.runner_grpc_channel_options,
    )
    add_service(resources, server)
    if len({settings.port, settings.health_port, settings.runner_channel_port}) != 3:
        raise ValueError("gRPC, runner channel and health ports must differ")
    # A separate listener, so the network fence can admit runners to the channel and nothing else.
    channel_server = grpc.aio.server()
    runner_channel_pb2_grpc.add_RunnerChannelServicer_to_server(
        RunnerChannelService(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(api),
                audience=settings.runner_channel_audience,
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
        channel_server,
    )
    channel_server.add_insecure_port(f"{settings.host}:{settings.runner_channel_port}")
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(session_changes.listener.listen())
        await stack.enter_async_context(delivery.listener.listen())
        await stack.enter_async_context(started(channel_server))
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
            await stack.enter_async_context(
                gateway(settings, api, server, commands_engine, sealer_accounts=settings.sealer_accounts)
            )
            server.add_insecure_port(f"{settings.host}:{settings.port}")
            await stack.enter_async_context(started(server))
            await serve_health(settings.host, settings.health_port)
    finally:
        await commands_engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
