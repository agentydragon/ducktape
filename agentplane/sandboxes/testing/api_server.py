"""Sandbox inventory and Pod-bound identities on a real Kubernetes API server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP, VERSION
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver
from util.kubernetes import CustomObjectsClient

NAMESPACE = "agentplane-acceptance"
SANDBOX = "test-runner"


@dataclass(frozen=True)
class SandboxCluster:
    server: KubernetesApiServer
    inventory: SandboxInventory
    uid: str

    @property
    def owner(self) -> ServiceAccountRef:
        return ServiceAccountRef(namespace=NAMESPACE, name=SANDBOX)


async def _workload(
    core: k8s_client.CoreV1Api, name: str, *, owner: k8s_client.V1OwnerReference | None = None
) -> k8s_client.V1Pod:
    await core.create_namespaced_service_account(
        NAMESPACE, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=name))
    )
    return await core.create_namespaced_pod(
        NAMESPACE,
        k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name=name, owner_references=[owner] if owner else None),
            spec=k8s_client.V1PodSpec(
                service_account_name=name,
                containers=[k8s_client.V1Container(name="workload", image="registry.test/unused")],
            ),
        ),
    )


@asynccontextmanager
async def sandbox_cluster() -> AsyncIterator[SandboxCluster]:
    """Create the controller's Ready Sandbox/Pod records; no runner container is scheduled."""
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api))
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        created = await custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            sandbox_resource.SANDBOXES_PLURAL,
            {
                "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
                "kind": sandbox_resource.SANDBOX_KIND,
                "metadata": {"name": SANDBOX},
                "spec": {"template": "test-template", "running": True},
            },
        )
        uid = created["metadata"]["uid"]
        pod = await _workload(
            core,
            SANDBOX,
            owner=k8s_client.V1OwnerReference(
                api_version=sandbox_resource.SANDBOX_API_VERSION,
                kind=sandbox_resource.SANDBOX_KIND,
                name=SANDBOX,
                uid=uid,
                controller=True,
            ),
        )
        await core.patch_namespaced_pod_status(
            SANDBOX,
            NAMESPACE,
            {"status": {"phase": "Running", "podIP": "127.0.0.1", "conditions": [{"type": "Ready", "status": "True"}]}},
        )
        await custom.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            NAMESPACE,
            sandbox_resource.SANDBOXES_PLURAL,
            SANDBOX,
            {
                "status": {
                    "operatingMode": sandbox_resource.OperatingMode.RUNNING.value,
                    "incarnation": {"name": SANDBOX, "uid": pod.metadata.uid},
                    "conditions": [
                        {
                            "type": sandbox_resource.READY,
                            "status": "True",
                            "reason": "Ready",
                            "message": "acceptance fixture",
                            "lastTransitionTime": datetime.now(UTC).isoformat(),
                        }
                    ],
                }
            },
            _content_type="application/merge-patch+json",
        )
        for name in ("notifications", "other"):
            await _workload(core, name)
        inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=custom, core_v1=core)
        yield SandboxCluster(server=server, inventory=inventory, uid=uid)
