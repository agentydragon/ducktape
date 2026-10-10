"""Real Kubernetes clients against a local API server, with no integration-app fixtures."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_views import MANAGED_LABEL
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.grpc_service import service_client
from agentplane.sandbox_service.testing.history import with_history
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, TokenVerdict, fake_apiserver, pod_for
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOX_API, SANDBOXES_PLURAL
from util.kubernetes import CustomObjectsClient

SANDBOX = "test-runner"
SANDBOX_UID = "40e373bd-2742-43de-8d1c-1ef97c4d4801"
ACCOUNT = "test-runner-account"


@dataclass
class Cluster:
    fake: FakeApiServer
    api: k8s_client.ApiClient
    provisioning: Provisioning

    @property
    def inventory(self) -> SandboxInventory:
        return self.provisioning.inventory


@asynccontextmanager
async def kubernetes() -> AsyncIterator[Cluster]:
    async with fake_apiserver() as fake:
        fake.put(
            SANDBOXES_PLURAL,
            {
                "apiVersion": SANDBOX_API.api_version,
                "kind": "Sandbox",
                "metadata": {
                    "name": SANDBOX,
                    "namespace": SANDBOX_NAMESPACE,
                    "uid": str(SANDBOX_UID),
                    "creationTimestamp": "2026-09-01T11:00:00Z",
                    "labels": {MANAGED_LABEL: "true"},
                },
                "spec": {"podTemplate": {"spec": {"serviceAccountName": ACCOUNT}}},
            },
        )
        pod = pod_for(fake, SANDBOX, pod_uid="test-pod-uid", ip="127.0.0.1")
        pod["spec"] = {
            "serviceAccountName": ACCOUNT,
            "containers": [{"name": "runner", "image": "registry.test/runner:unused"}],
        }
        pod["status"] |= {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        fake.pods[SANDBOX] = pod
        configuration = k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")
        async with k8s_client.ApiClient(configuration) as api:
            custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
            inventory = SandboxInventory(
                namespace=SANDBOX_NAMESPACE, custom_objects=custom, core_v1=k8s_client.CoreV1Api(api)
            )
            yield Cluster(
                fake,
                api,
                Provisioning(
                    inventory,
                    EgressInventory(namespace=SANDBOX_NAMESPACE, custom_objects=custom),
                    ActionPolicyBindings(namespace=SANDBOX_NAMESPACE, custom_objects=custom),
                    {},
                    KubernetesBindings(inventory, k8s_client.RbacAuthorizationV1Api(api)),
                ),
            )


@asynccontextmanager
async def authenticated_service(
    cluster: Cluster,
    runner_port: int,
    token_file: Path,
    *,
    history_database_url: str | None = None,
    manager: ServiceAccountRef | None = None,
    token: str = "test-app-service-token",
    audience: str = "test-app-sandbox-service",
    platform_instructions: str = "Backend guidance for app-launched sessions.",
) -> AsyncIterator[SandboxServiceClient]:
    manager = manager or ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-app")
    await asyncio.to_thread(token_file.write_text, token)
    cluster.fake.tokens[token] = TokenVerdict(
        username=f"system:serviceaccount:{manager.namespace}:{manager.name}",
        pod_name="test-app-pod",
        pod_uid="test-app-pod-uid",
        audiences=(audience,),
    )
    resources = Resources(
        runner_admission_ack_timeout_s=1,
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=audience,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), runner_port),
        provisioning=cluster.provisioning,
        caller_accounts=frozenset({manager}),
        platform_instructions=platform_instructions,
        follow_lease_s=1,
    )
    async with (
        with_history(resources, history_database_url) as configured,
        service_client(configured, token_file) as client,
    ):
        yield client
