"""Standalone Sandbox Service entry point: provisions, grants and tears down Sandboxes. Kubernetes
owns inventory; the Runner Gateway serves their Sessions."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_process import background, kubernetes_configuration, serve_health, serving
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
from agentplane.sandbox_service.settings import Settings
from agentplane.sandbox_service.teardown import GatewaySealing, Teardown
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


@asynccontextmanager
async def sandboxes(
    settings: Settings, api: k8s_client.ApiClient, server: grpc.aio.Server, *, sealing: GatewaySealing
) -> AsyncIterator[None]:
    """Serve `SandboxService` on `server` and reconcile Sandboxes, sealing each teardown with `sealing`."""
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
        SandboxController(provisioning, Teardown(inventory, sealing)).run(), name="sandbox-provisioning"
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

    async with (
        k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
        AsyncExitStack() as stack,
    ):
        await stack.enter_async_context(gateway)
        server = grpc.aio.server()
        await stack.enter_async_context(sandboxes(settings, api, server, sealing=GatewaySealing(gateway)))
        await stack.enter_async_context(serving(server, f"{settings.host}:{settings.port}", grace_s=5))
        await serve_health(settings.host, settings.health_port)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
