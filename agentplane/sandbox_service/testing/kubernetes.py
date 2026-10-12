"""Real Kubernetes clients against a local API server, with no integration-app fixtures."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP
from agentplane.grpc_calls import dial
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxDestination, ServiceAccount
from agentplane.runner.runner import Runner
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.testing.control_listener import control_plane, dialing, wait_connected
from agentplane.runner_gateway.testing.grpc_service import gateway_client, service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.binding_storage import write_binding
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_views import MANAGED_LABEL, SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.fake_inventory import RUNNER_ENDPOINTS
from agentplane.sandbox_service.testing.grpc_service import sandbox_client
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, TokenVerdict, fake_apiserver, pod_for
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOX_API, SANDBOXES_PLURAL
from util.kubernetes import CustomObjectsClient
from util.testing.sqlalchemy_engines import postgres_async_engine

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-runner"
SANDBOX_UID = "40e373bd-2742-43de-8d1c-1ef97c4d4801"
ACCOUNT = "test-runner-account"
OWNER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
# The fake's store of Agentplane's own Sandboxes, apart from agent-sandbox's of the same plural.
OWN_SANDBOXES = f"{sandbox_resource.SANDBOXES_PLURAL}.{GROUP}"
_POD_UID = "test-pod-uid"
_RUNNER_AUDIENCE = "test-app-runner-control"
_RUNNER_TOKEN = "test-app-runner-token"
DESTINATION = SandboxDestination(
    owner=ServiceAccount(namespace=OWNER.namespace, name=OWNER.name), sandbox=SANDBOX, sandbox_uid=SANDBOX_UID
)


@dataclass
class Cluster:
    fake: FakeApiServer
    api: k8s_client.ApiClient
    provisioning: Provisioning

    @property
    def inventory(self) -> SandboxInventory:
        return self.provisioning.inventory

    def set_binding(self, binding: SandboxBinding) -> None:
        self.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"].setdefault("annotations", {})[
            SANDBOX_BINDING_ANNOTATION
        ] = write_binding(binding)

    def build_principal_resolver(self, audience: str) -> WorkloadPrincipalResolver:
        """Callers authenticate to this audience with tokens the fake TokenReview trusts."""
        return WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(self.api),
            audience=audience,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        )


@asynccontextmanager
async def kubernetes() -> AsyncIterator[Cluster]:
    # Everything the service reads or writes through this server is in the Sandbox namespace.
    async with fake_apiserver(
        dict.fromkeys((SANDBOXES_PLURAL, OWN_SANDBOXES, "leases", "services", "endpointslices"), SANDBOX_NAMESPACE)
    ) as fake:
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
        pod = pod_for(fake, SANDBOX, pod_uid=_POD_UID, ip="127.0.0.1")
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
                    RUNNER_ENDPOINTS,
                    own_sandboxes=None,
                ),
            )


@asynccontextmanager
async def authenticated_service(
    cluster: Cluster,
    runner: Runner,
    token_file: Path,
    *,
    database_url: str,
    manager: ServiceAccountRef | None = None,
    token: str = "test-app-service-token",
    audience: str = "test-app-sandbox-service",
    platform_instructions: str = "Backend guidance for app-launched sessions.",
) -> AsyncIterator[tuple[SandboxServiceClient, RunnerGatewayClient]]:
    """The Sandbox Service and gateway over `cluster`, with `runner` as `SANDBOX`'s runner, connected over the
    control stream."""
    manager = manager or ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-app")
    await asyncio.to_thread(token_file.write_text, token)
    cluster.fake.tokens[token] = TokenVerdict(
        username=f"system:serviceaccount:{manager.namespace}:{manager.name}",
        pod_name="test-app-pod",
        pod_uid="test-app-pod-uid",
        audiences=(audience,),
    )
    async with postgres_async_engine(database_url) as engine:
        resources = build_resources(
            engine,
            principals=cluster.build_principal_resolver(audience),
            destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
            caller_accounts=frozenset({manager}),
            platform_instructions=platform_instructions,
            # Native acceptance uses production's admission budget, including delivery's
            # two-second recheck. The gateway unit fixture keeps its one-second default.
            runner_admission_ack_timeout_s=15,
        )
        async with (
            runner_connected(cluster, resources, runner),
            service(resources, cluster.provisioning) as target,
            dial(target) as channel,
        ):
            yield (
                sandbox_client(channel, token_file, namespace=cluster.inventory.namespace),
                gateway_client(
                    channel,
                    token_file,
                    command_admission_timeout_s=resources.runner_admission_ack_timeout_s + 10,
                    control_target=target,
                ),
            )


@asynccontextmanager
async def runner_connected(cluster: Cluster, resources: Resources, runner: Runner) -> AsyncIterator[None]:
    """`runner`, as `SANDBOX`'s, holds a control stream to the gateway `resources` back."""
    cluster.fake.tokens[_RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid=_POD_UID,
        audiences=(_RUNNER_AUDIENCE,),
    )
    async with (
        control_plane(
            resources, principals=cluster.build_principal_resolver(_RUNNER_AUDIENCE), api=cluster.api
        ) as target,
        dialing(runner, target, _RUNNER_TOKEN),
    ):
        await wait_connected(resources, UUID(SANDBOX_UID))
        yield
