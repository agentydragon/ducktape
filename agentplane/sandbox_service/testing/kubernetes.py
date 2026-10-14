"""Real Kubernetes clients against a local API server, with no integration-app fixtures."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.proto.sandboxes_pb2 import SandboxBinding, SandboxDestination, ServiceAccount
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, FakeApiServer, fake_apiserver, pod_for
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-runner"
SANDBOX_UID = "40e373bd-2742-43de-8d1c-1ef97c4d4801"
# The Sandbox Controller runs a Sandbox's Pods as the ServiceAccount of its name.
ACCOUNT = SANDBOX
OWNER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
POD_UID = "test-pod-uid"
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
        spec = self.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]
        if binding.HasField("session_defaults"):
            spec["sessionDefaults"] = MessageToDict(binding.session_defaults)
        if binding.bootstrap:
            spec["bootstrap"] = binding.bootstrap

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
        dict.fromkeys((sandbox_resource.SANDBOXES_PLURAL, "leases", "services", "endpointslices"), SANDBOX_NAMESPACE)
    ) as fake:
        fake.put(
            sandbox_resource.SANDBOXES_PLURAL,
            {
                "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
                "kind": sandbox_resource.SANDBOX_KIND,
                "metadata": {
                    "name": SANDBOX,
                    "namespace": SANDBOX_NAMESPACE,
                    "uid": SANDBOX_UID,
                    "creationTimestamp": "2026-09-01T11:00:00Z",
                },
                "spec": {"template": "test-template", "running": True},
            },
        )
        pod = pod_for(fake, SANDBOX, pod_uid=POD_UID, ip="127.0.0.1")
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
                    default_policy_sets=frozenset(),
                ),
            )
