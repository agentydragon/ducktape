"""Kubernetes Policy Controller entry point."""

import asyncio
from typing import cast

from kubernetes_asyncio import client as k8s_client

from agentplane.kubernetes_policy.controller import KubernetesPolicyController
from agentplane.kubernetes_policy.settings import Settings
from agentplane.service_process import configure_logging, kubernetes_configuration
from util.kubernetes import CustomObjectsClient


async def run(settings: Settings) -> None:
    async with k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api:
        await KubernetesPolicyController(
            custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api)),
            core_v1=k8s_client.CoreV1Api(api),
            rbac=k8s_client.RbacAuthorizationV1Api(api),
            policy_namespace=settings.policy_namespace,
            subject_namespaces=settings.subject_namespaces,
            resync_seconds=settings.resync_seconds,
        ).run()


def main() -> None:
    configure_logging()
    asyncio.run(run(Settings()))


if __name__ == "__main__":
    main()
