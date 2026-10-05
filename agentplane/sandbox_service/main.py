"""Standalone Sandbox Service entry point. Kubernetes owns inventory; runners own session logs."""

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources, add_service
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import ClusterRoleBindingGrant, KubernetesGrant, RoleBindingGrant
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

CONFIG_FILE_ENV = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SANDBOX_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    platform_instructions: str = Field(min_length=1)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    default_policies: list[str] = Field(default_factory=list)
    kubernetes_grants: dict[str, KubernetesGrant] = Field(default_factory=dict)
    kubernetes_binding_cleanup_namespaces: set[str] = Field(default_factory=set)
    kubernetes_cluster_binding_cleanup: bool = False
    token_audience: str = "agentplane-sandbox-service"
    runner_port: int = Field(default=7000, ge=1, le=65535)
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    follow_lease_s: float = Field(default=900, gt=0, le=900)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    health_port: int = Field(default=8081, ge=1, le=65535)
    kubeconfig: Path | None = None

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


async def serve(settings: Settings) -> None:
    platform_instructions = settings.platform_instructions
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    async with k8s_client.ApiClient(configuration) as api:
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
                namespace=settings.sandbox_namespace, custom_objects=custom, default_policies=settings.default_policies
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
        resources = Resources(
            principals=principals,
            destinations=DestinationResolver(inventory, core, settings.runner_port),
            admission_timeout_s=settings.admission_timeout_s,
            follow_lease_s=settings.follow_lease_s,
            caller_accounts=settings.caller_accounts,
            platform_instructions=platform_instructions,
            lifecycle_timeout_s=settings.lifecycle_timeout_s,
            provisioning=provisioning,
        )
        if settings.port == settings.health_port:
            raise ValueError("gRPC and health ports must differ")
        server = grpc.aio.server()
        add_service(resources, server)
        server.add_insecure_port(f"{settings.host}:{settings.port}")
        await server.start()
        health = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

        @health.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        reconcile = asyncio.create_task(provisioning.run(), name="sandbox-provisioning")
        try:
            await uvicorn.Server(
                uvicorn.Config(health, host=settings.host, port=settings.health_port, access_log=False)
            ).serve()
        finally:
            await server.stop(grace=5)
            reconcile.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
