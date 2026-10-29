"""A running Sandbox and its runner Pod on a real Kubernetes API server, recorded as the Sandbox
Controller records them.

No kubelet or controller runs: the Pod's and the Sandbox's statuses are whatever this module and
the test write.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP, VERSION
from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.launch import SandboxIdentity
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import runner_identity_uri
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf

SANDBOX = "test-runner"
_MERGE_PATCH = "application/merge-patch+json"
_RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/runner-credential"
_RUNNER_CREDENTIAL_ID = "test-runner-credential"


@dataclass(frozen=True)
class SandboxCluster:
    server: KubernetesApiServer
    # The cluster's own namespace, so that tests can share one control plane.
    namespace: str
    # The Sandbox's, its Pod's and its ServiceAccount's.
    name: str
    inventory: SandboxInventory
    # The UIDs the API server gave the Sandbox and its Pod.
    uid: str
    pod_uid: str

    @property
    def identity(self) -> SandboxIdentity:
        return SandboxIdentity(namespace=self.namespace, name=self.name, uid=self.uid)

    @property
    def owner(self) -> ServiceAccountRef:
        """The Sandbox's own ServiceAccount, which its runner Pod runs as."""
        return ServiceAccountRef(namespace=self.namespace, name=self.name)

    @property
    def destination(self) -> SandboxDestination:
        return SandboxDestination(
            owner=ServiceAccount(namespace=self.namespace, name=self.name), sandbox=self.name, sandbox_uid=self.uid
        )

    @property
    def runner_identity_uri(self) -> str:
        return runner_identity_uri(self.namespace, self.uid, _RUNNER_CREDENTIAL_ID)

    async def pod_token(self, audience: str) -> str:
        """A token bound to the runner Pod, as its runner container holds one."""
        return await self.server.pod_token(self.namespace, self.name, [audience])

    @property
    def custom_objects(self) -> CustomObjectsClient:
        return cast(CustomObjectsClient, k8s_client.CustomObjectsApi(self.server.api))

    async def sandbox(self) -> dict[str, Any]:
        """The Sandbox as stored."""
        return await self.custom_objects.get_namespaced_custom_object(
            GROUP, VERSION, self.namespace, sandbox_resource.SANDBOXES_PLURAL, self.name
        )

    async def patch_sandbox(self, patch: dict[str, Any]) -> None:
        await self.custom_objects.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.namespace,
            sandbox_resource.SANDBOXES_PLURAL,
            self.name,
            patch,
            _content_type=_MERGE_PATCH,
        )

    async def patch_sandbox_status(self, patch: dict[str, Any]) -> None:
        """Merge `patch` into the status, which in a cluster only the Sandbox Controller writes."""
        await self.custom_objects.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            self.namespace,
            sandbox_resource.SANDBOXES_PLURAL,
            self.name,
            patch,
            _content_type=_MERGE_PATCH,
        )


@asynccontextmanager
async def sandbox_cluster(server: KubernetesApiServer, *, name: str = SANDBOX) -> AsyncIterator[SandboxCluster]:
    """A namespace of its own on `server` with one Ready, running Sandbox `name` and its runner Pod at 127.0.0.1."""
    namespace = f"sandboxes-{uuid4().hex[:8]}-test"
    core = k8s_client.CoreV1Api(server.api)
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api))
    await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
    created = await custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        namespace,
        sandbox_resource.SANDBOXES_PLURAL,
        {
            "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
            "kind": sandbox_resource.SANDBOX_KIND,
            "metadata": {"name": name},
            "spec": {"template": "test-template", "running": True},
        },
    )
    uid = created["metadata"]["uid"]
    # The Sandbox Controller runs a Sandbox's Pods as the ServiceAccount of its name.
    await core.create_namespaced_service_account(
        namespace,
        k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version=sandbox_resource.SANDBOX_API_VERSION,
                        kind=sandbox_resource.SANDBOX_KIND,
                        name=name,
                        uid=uid,
                        controller=True,
                    )
                ],
            )
        ),
    )
    pod = await core.create_namespaced_pod(
        namespace,
        k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels={_RUNNER_CREDENTIAL_LABEL: _RUNNER_CREDENTIAL_ID},
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version=sandbox_resource.SANDBOX_API_VERSION,
                        kind=sandbox_resource.SANDBOX_KIND,
                        name=name,
                        uid=uid,
                        controller=True,
                    )
                ],
            ),
            spec=k8s_client.V1PodSpec(
                service_account_name=name,
                containers=[k8s_client.V1Container(name="runner", image="registry.test/runner:unused")],
            ),
        ),
    )
    await core.patch_namespaced_pod_status(
        name,
        namespace,
        {"status": {"phase": "Running", "podIP": "127.0.0.1", "conditions": [{"type": "Ready", "status": "True"}]}},
    )
    cluster = SandboxCluster(
        server=server,
        namespace=namespace,
        name=name,
        inventory=SandboxInventory(namespace=namespace, custom_objects=custom, core_v1=core),
        uid=uid,
        pod_uid=pod.metadata.uid,
    )
    await cluster.patch_sandbox_status(
        {
            "status": {
                "operatingMode": sandbox_resource.OperatingMode.RUNNING.value,
                "incarnation": {"name": name, "uid": pod.metadata.uid},
                "conditions": [
                    {
                        "type": sandbox_resource.READY,
                        "status": "True",
                        "reason": "PodReady",
                        "message": "",
                        "lastTransitionTime": "2026-09-01T11:00:00Z",
                    }
                ],
            }
        }
    )
    yield cluster
