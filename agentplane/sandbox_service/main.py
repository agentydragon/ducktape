"""Standalone Sandbox Service entry point: provisions, grants and tears down Sandboxes. Kubernetes
owns inventory; the Runner Gateway serves their Sessions."""

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config

from agentplane.runner_gateway import main as gateway_main
from agentplane.runner_gateway.grpc_api import add_legacy_service
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.controller import SandboxController
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources, add_service
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import ClusterRoleBindingGrant, RoleBindingGrant
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoints
from agentplane.sandbox_service.settings import CombinedSettings, Launch, Settings
from agentplane.sandbox_service.teardown import Seal, Teardown
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


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
async def sandboxes(
    settings: Settings | CombinedSettings, api: k8s_client.ApiClient, server: grpc.aio.Server, *, seal: Seal
) -> AsyncIterator[None]:
    """Serve `SandboxService` on `server` and reconcile Sandboxes, sealing each teardown with `seal`."""
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
    inventory = SandboxInventory(
        namespace=settings.sandbox_namespace, core_v1=k8s_client.CoreV1Api(api), custom_objects=custom
    )
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
        RunnerEndpoints(control=settings.runner_control, ingress=settings.runner_ingress),
    )
    add_service(
        Resources(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(api),
                audience=settings.token_audience,
                allowed_service_account_namespaces={account.namespace for account in settings.caller_accounts},
            ),
            provisioning=provisioning,
            caller_accounts=settings.caller_accounts,
            admission_timeout_s=settings.admission_timeout_s,
            lifecycle_timeout_s=settings.lifecycle_timeout_s,
        ),
        server,
    )
    async with background(
        SandboxController(provisioning, Teardown(inventory, seal)).run(), name="sandbox-provisioning"
    ):
        yield


async def serve(settings: Settings) -> None:
    gateway = SandboxServiceClient(
        f"127.0.0.1:{settings.port}",
        gateway_target=settings.gateway_target,
        control_target=settings.control_target,
        namespace=settings.sandbox_namespace,
        token_file=settings.gateway_token_file,
        command_admission_timeout_s=None,
        request_timeout_s=settings.admission_timeout_s,
        lifecycle_timeout_s=settings.lifecycle_timeout_s,
        follow_timeout_s=settings.lifecycle_timeout_s,
    )

    async def seal(sandbox: protocol_pb2.Sandbox) -> dict[str, int]:
        destination = protocol_pb2.SandboxDestination(
            owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid
        )
        return await gateway.runner(destination).seal()

    async with (
        k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
        AsyncExitStack() as stack,
    ):
        stack.push_async_callback(gateway.close)
        server = grpc.aio.server()
        await stack.enter_async_context(sandboxes(settings, api, server, seal=seal))
        server.add_insecure_port(f"{settings.host}:{settings.port}")
        await stack.enter_async_context(started(server))
        await serve_health(settings.host, settings.health_port)


async def serve_combined(settings: CombinedSettings) -> None:
    """The Sandbox Service and Runner Gateway in one process, sealing in-process. See
    `Launch.gateway_target`."""
    commands_engine = gateway_main.database_engine(settings.commands_database_url)
    try:
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
            AsyncExitStack() as stack,
        ):
            server = grpc.aio.server()
            gateway = await stack.enter_async_context(
                gateway_main.gateway(settings, api, server, commands_engine, sealer_accounts=frozenset())
            )
            await stack.enter_async_context(sandboxes(settings, api, server, seal=gateway.sealing.seal))
            add_legacy_service(gateway, server)
            server.add_insecure_port(f"{settings.host}:{settings.port}")
            await stack.enter_async_context(started(server))
            await serve_health(settings.host, settings.health_port)
    finally:
        await commands_engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve_combined(CombinedSettings()) if Launch().gateway_target is None else serve(Settings()))


if __name__ == "__main__":
    main()
