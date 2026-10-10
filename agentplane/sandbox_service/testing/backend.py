"""Run the real authenticated service boundary over in-memory Kubernetes for consumer tests.

The server has its own event loop so synchronous HTTP TestClients cannot starve gRPC.
Only the endpoint and public client escape this fixture, never provisioning implementations.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from anyio.from_thread import start_blocking_portal
from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    pod,
    sandbox,
)
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac
from agentplane.sandbox_service.testing.grpc_service import service
from agentplane.sandbox_service.testing.history import with_history
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOX_API
from util.kubernetes import CustomObjectsClient

TOKEN = "test-consumer-sandbox-service-token"
AUDIENCE = "test-consumer-sandbox-service"
MANAGER = ServiceAccountRef(namespace=NAMESPACE, name="test-integration-app")


class Authentication:
    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=body.spec.token == TOKEN,
                audiences=[AUDIENCE],
                user=k8s_client.V1UserInfo(
                    username=f"system:serviceaccount:{MANAGER.namespace}:{MANAGER.name}",
                    extra={POD_NAME_CLAIM: ["test-app-pod"], POD_UID_CLAIM: ["test-app-pod-uid"]},
                ),
            ),
        )


@dataclass(frozen=True)
class Endpoint:
    target: str
    token_file: Path
    reconcile: Callable[[], None]

    def client(self) -> SandboxServiceClient:
        return SandboxServiceClient(
            self.target,
            namespace=NAMESPACE,
            token_file=self.token_file,
            command_admission_timeout_s=20,
            request_timeout_s=20,
            lifecycle_timeout_s=310,
            follow_timeout_s=960,
        )


@contextmanager
def backend(
    custom_objects: FakeCustomObjectsApi,
    core: FakeCoreV1Api,
    token_file: Path,
    *,
    runner_port: int = 1,
    history_database_url: str | None = None,
    default_policies: Sequence[str] = (),
    grants: dict[str, KubernetesGrant] | None = None,
    rbac: FakeRbac | None = None,
    platform_instructions: str = "Test backend guidance.",
) -> Iterator[Endpoint]:
    token_file.write_text(TOKEN)
    custom = cast(CustomObjectsClient, custom_objects)
    core_api = cast(k8s_client.CoreV1Api, core)
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=custom, core_v1=core_api)
    bindings = KubernetesBindings(inventory, cast(k8s_client.RbacAuthorizationV1Api, rbac or FakeRbac()))
    resources = Resources(
        runner_admission_ack_timeout_s=1,
        principals=WorkloadPrincipalResolver(
            authentication=cast(k8s_client.AuthenticationV1Api, Authentication()),
            audience=AUDIENCE,
            allowed_service_account_namespaces={NAMESPACE},
        ),
        destinations=DestinationResolver(inventory, core_api, runner_port),
        caller_accounts=frozenset({MANAGER}),
        platform_instructions=platform_instructions,
        provisioning=Provisioning(
            inventory,
            EgressInventory(namespace=NAMESPACE, custom_objects=custom, default_policies=default_policies),
            ActionPolicyBindings(namespace=NAMESPACE, custom_objects=custom),
            grants if grants is not None else {},
            bindings,
        ),
    )
    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(with_history(resources, history_database_url)) as configured,
        portal.wrap_async_context_manager(service(configured)) as target,
    ):
        yield Endpoint(target, token_file, lambda: portal.call(resources.provisioning.reconcile_once))


def seed_runner(
    custom: FakeCustomObjectsApi, core: FakeCoreV1Api, name: str
) -> tuple[dict[str, Any], k8s_client.V1Pod]:
    """Controller-shaped Sandbox and Pod, suitable for real destination authorization."""
    raw = sandbox(name)
    running = pod(name, phase="Running", ready=True, ip="127.0.0.1")
    running.metadata.namespace = NAMESPACE
    running.metadata.uid = f"test-pod-{name}"
    running.metadata.owner_references = [
        k8s_client.V1OwnerReference(
            api_version=SANDBOX_API.api_version, kind="Sandbox", name=name, uid=raw["metadata"]["uid"], controller=True
        )
    ]
    running.spec.service_account_name = name
    custom.objects[("sandboxes", name)] = raw
    core.pods[name] = running
    return raw, running
