"""Standalone Sandbox Service entry point. Kubernetes owns inventory; runners own session logs."""

import asyncio
import logging
import socket
from contextlib import suppress
from typing import cast

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.runner_channel import protocol_pb2_grpc
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.admission_reconciliation import AdmissionReconciler
from agentplane.sandbox_service.channel_delivery import ChannelDelivery
from agentplane.sandbox_service.commands.connections import ConnectionStore
from agentplane.sandbox_service.commands.dispatch import DispatchStore
from agentplane.sandbox_service.commands.store import SubmissionStore
from agentplane.sandbox_service.controller import SandboxController
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources, add_service
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import ClusterRoleBindingGrant, RoleBindingGrant
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.runner_channel import ChannelTiming, RunnerChannelService
from agentplane.sandbox_service.session_history.ingestion import HistoryIngester
from agentplane.sandbox_service.session_history.session_changes import SessionChanges
from agentplane.sandbox_service.session_history.store import Store
from agentplane.sandbox_service.session_registry import SessionRegistry
from agentplane.sandbox_service.settings import Settings
from agentplane.sandbox_service.teardown import Teardown
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg
# The channel servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/runner_channel:protocol_pb2
# gazelle:include_dep @pypi//protobuf


async def serve(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2
    )
    commands_engine = create_async_engine(
        make_url(settings.commands_database_url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2
    )
    try:
        await serve_with_engines(settings, configuration, engine, commands_engine)
    finally:
        await engine.dispose()
        await commands_engine.dispose()


async def serve_with_engines(
    settings: Settings, configuration: k8s_client.Configuration, engine: AsyncEngine, commands_engine: AsyncEngine
) -> None:
    session_changes = SessionChanges(commands_engine.url)
    async with k8s_client.ApiClient(configuration) as api, session_changes.listener.listen():
        core = k8s_client.CoreV1Api(api)
        inventory = SandboxInventory(
            namespace=settings.sandbox_namespace,
            core_v1=core,
            custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api)),
        )
        principals = WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces={a.namespace for a in settings.caller_accounts},
        )
        custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
        provisioning = Provisioning(
            inventory,
            EgressInventory(
                namespace=settings.sandbox_namespace,
                custom_objects=custom,
                default_policies=settings.default_egress_policies,
            ),
            ActionPolicyBindings(namespace=settings.sandbox_namespace, custom_objects=custom),
            settings.kubernetes_grants,
            KubernetesBindings(
                inventory,
                k8s_client.RbacAuthorizationV1Api(api),
                cleanup_namespaces=(
                    settings.kubernetes_binding_cleanup_namespaces
                    | {
                        grant.namespace
                        for grant in settings.kubernetes_grants.values()
                        if isinstance(grant, RoleBindingGrant)
                    }
                )
                - {settings.sandbox_namespace},
                cleanup_cluster_bindings=settings.kubernetes_cluster_binding_cleanup
                or any(isinstance(grant, ClusterRoleBindingGrant) for grant in settings.kubernetes_grants.values()),
            ),
        )
        history_store = Store(engine)
        sessions = SessionRegistry(commands_engine)
        submissions = SubmissionStore(commands_engine)
        connections = ConnectionStore(commands_engine)
        timing = ChannelTiming()
        delivery = ChannelDelivery(
            commands_engine.url,
            dispatches=DispatchStore(commands_engine),
            connections=connections,
            liveness_s=timing.liveness_s,
        )
        resources = Resources(
            principals=principals,
            sessions=sessions,
            submissions=submissions,
            delivery=delivery,
            session_changes=session_changes,
            destinations=DestinationResolver(inventory, core, settings.runner_port),
            admission_timeout_s=settings.admission_timeout_s,
            runner_admission_ack_timeout_s=settings.runner_admission_ack_timeout_s,
            follow_lease_s=settings.follow_lease_s,
            caller_accounts=settings.caller_accounts,
            platform_instructions=settings.platform_instructions,
            lifecycle_timeout_s=settings.lifecycle_timeout_s,
            runner_grpc_channel_options=settings.runner_grpc_channel_options,
            provisioning=provisioning,
        )
        async with delivery.listener.listen():
            if len({settings.port, settings.health_port, settings.runner_channel_port}) != 3:
                raise ValueError("gRPC, runner channel and health ports must differ")
            server = grpc.aio.server()
            add_service(resources, server)
            server.add_insecure_port(f"{settings.host}:{settings.port}")
            await server.start()
            # A separate listener, so the network fence can admit runners to the channel and nothing else.
            channel_server = grpc.aio.server()
            protocol_pb2_grpc.add_RunnerChannelServicer_to_server(
                RunnerChannelService(
                    principals=WorkloadPrincipalResolver(
                        authentication=k8s_client.AuthenticationV1Api(api),
                        audience=settings.runner_channel_audience,
                        allowed_service_account_namespaces={settings.sandbox_namespace},
                    ),
                    inventory=inventory,
                    core=core,
                    connections=connections,
                    delivery=delivery,
                    replica=socket.gethostname(),  # the Pod name
                    timing=timing,
                ),
                channel_server,
            )
            channel_server.add_insecure_port(f"{settings.host}:{settings.runner_channel_port}")
            await channel_server.start()
            health = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

            @health.get("/healthz")
            async def healthz() -> dict[str, str]:
                return {"status": "ok"}

            teardown = Teardown(
                resources.destinations, sessions, runner_grpc_channel_options=settings.runner_grpc_channel_options
            )
            reconcile = asyncio.create_task(
                SandboxController(provisioning, teardown).run(), name="sandbox-provisioning"
            )
            admissions = asyncio.create_task(
                AdmissionReconciler(
                    submissions,
                    sessions,
                    resources.destinations,
                    runner_grpc_channel_options=settings.runner_grpc_channel_options,
                ).run(),
                name="sandbox-admission-reconciliation",
            )
            history = (
                asyncio.create_task(
                    HistoryIngester(
                        history_store,
                        sessions,
                        submissions,
                        resources.destinations,
                        runner_grpc_channel_options=settings.runner_grpc_channel_options,
                    ).run(),
                    name="sandbox-session-history-ingestion",
                )
                if settings.ingest_history
                else None
            )
            try:
                await uvicorn.Server(
                    uvicorn.Config(health, host=settings.host, port=settings.health_port, access_log=False)
                ).serve()
            finally:
                await server.stop(grace=5)
                await channel_server.stop(grace=5)
                for task in (reconcile, admissions):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                if history is not None:
                    history.cancel()
                    with suppress(asyncio.CancelledError):
                        await history


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
