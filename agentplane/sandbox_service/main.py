"""Standalone Sandbox Service entry point: creates, suspends, resumes and deletes Sandboxes, and grants
them egress. Kubernetes owns inventory; the Sandbox Controller reconciles them and the Runner Gateway
serves their Sessions."""

import asyncio
from contextlib import AsyncExitStack
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_serving import add_health, serving
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources, add_service
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.settings import Settings
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.service_process import configure_logging, kubernetes_configuration, until_terminated
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


def add_sandboxes(settings: Settings, api: k8s_client.ApiClient, server: grpc.aio.Server) -> None:
    """Serve `SandboxService` on `server`."""
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
    add_service(
        Resources(
            principals=WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(api),
                audience=settings.token_audience,
                allowed_service_account_namespaces={account.namespace for account in settings.caller_accounts},
            ),
            provisioning=Provisioning(
                SandboxInventory(
                    namespace=settings.sandbox_namespace, core_v1=k8s_client.CoreV1Api(api), custom_objects=custom
                ),
                EgressInventory(namespace=settings.sandbox_namespace, custom_objects=custom),
                default_policy_sets=settings.default_policy_sets,
            ),
            caller_accounts=settings.caller_accounts,
            admission_timeout_s=settings.admission_timeout_s,
            lifecycle_timeout_s=settings.lifecycle_timeout_s,
        ),
        server,
    )


async def serve(settings: Settings) -> None:
    async with (
        k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
        AsyncExitStack() as stack,
    ):
        server = grpc.aio.server()
        add_health(server)
        add_sandboxes(settings, api, server)
        await stack.enter_async_context(serving(server, f"{settings.host}:{settings.port}", grace_s=5))
        await until_terminated()


def main() -> None:
    configure_logging()
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
