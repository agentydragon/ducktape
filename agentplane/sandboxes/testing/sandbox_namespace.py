"""One test's namespace on a real Kubernetes API server, and the objects a Sandbox's readers find in it.

No controller, scheduler or kubelet runs: a Sandbox's or Pod's status is what the test writes through the
status subresource, and nothing is garbage-collected. Without its namespace controller the API server never
finishes deleting a namespace, so each test takes one of its own and leaves it with the control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.resources import CREDENTIALS_PLURAL
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.sandbox_resource import OperatingMode
from agentplane.sandboxes.egress_views import FLUX_KUSTOMIZATION_LABEL
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from util.kubernetes import CustomObjectsClient

TEMPLATE = "test-runner-template"
RUNNER_IMAGE = "registry.test/runner:test"
_MERGE_PATCH = "application/merge-patch+json"

READY: list[dict[str, Any]] = [
    {
        "type": "Ready",
        "status": "True",
        "reason": "WorkloadReady",
        "message": "",
        "lastTransitionTime": "2026-01-01T00:00:00Z",
    }
]
"""The conditions of a Sandbox whose controller reports its incarnation able to take work."""


@dataclass(frozen=True)
class SandboxNamespace:
    name: str
    custom_objects: CustomObjectsClient
    core_v1: k8s_client.CoreV1Api
    # How another process reaches the same API server.
    kubeconfig: Path

    @classmethod
    async def new(cls, server: KubernetesApiServer) -> SandboxNamespace:
        """A namespace of the test's own, holding the SandboxTemplate `TEMPLATE`."""
        core_v1 = k8s_client.CoreV1Api(server.api)
        name = f"test-{uuid4().hex[:12]}"
        await core_v1.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=name)))
        namespace = cls(
            name=name,
            custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api)),
            core_v1=core_v1,
            kubeconfig=server.kubeconfig,
        )
        await namespace._create(
            sandbox_resource.TEMPLATES_PLURAL,
            "SandboxTemplate",
            TEMPLATE,
            {
                "pod": {"spec": {"containers": [{"name": "runner", "image": RUNNER_IMAGE}]}},
                "stateVolume": {"name": "state", "claim": {}, "layout": {"type": "DirectoryTree"}},
            },
        )
        return namespace

    def account(self, name: str) -> ServiceAccountRef:
        """The ServiceAccount of this name here, which a Sandbox of the name runs as."""
        return ServiceAccountRef(namespace=self.name, name=name)

    async def read(self, plural: str, name: str) -> dict[str, Any]:
        return await self.custom_objects.get_namespaced_custom_object(GROUP, VERSION, self.name, plural, name)

    async def objects(self, plural: str) -> list[dict[str, Any]]:
        listed = await self.custom_objects.list_namespaced_custom_object(GROUP, VERSION, self.name, plural)
        return cast(list[dict[str, Any]], listed["items"])

    async def names(self, plural: str) -> set[str]:
        return {item["metadata"]["name"] for item in await self.objects(plural)}

    async def pods(self) -> list[k8s_client.V1Pod]:
        return cast(list[k8s_client.V1Pod], (await self.core_v1.list_namespaced_pod(self.name)).items)

    async def patch(self, plural: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """A merge patch of the object; its `status` is the status subresource's, which this leaves alone."""
        return cast(
            dict[str, Any],
            await self.custom_objects.patch_namespaced_custom_object(
                GROUP, VERSION, self.name, plural, name, body, _content_type=_MERGE_PATCH
            ),
        )

    async def patch_status(self, plural: str, name: str, status: dict[str, Any]) -> dict[str, Any]:
        """A merge patch of the status, as the object's controller or readers write it; `None` removes a field."""
        return cast(
            dict[str, Any],
            await self.custom_objects.patch_namespaced_custom_object_status(
                GROUP, VERSION, self.name, plural, name, {"status": status}, _content_type=_MERGE_PATCH
            ),
        )

    async def delete(self, plural: str, name: str) -> None:
        await self.custom_objects.delete_namespaced_custom_object(
            GROUP, VERSION, self.name, plural, name, body=k8s_client.V1DeleteOptions()
        )

    async def _create(self, plural: str, kind: str, name: str, spec: dict[str, Any], **metadata: Any) -> dict[str, Any]:
        return await self.custom_objects.create_namespaced_custom_object(
            GROUP,
            VERSION,
            self.name,
            plural,
            {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": {"name": name, **metadata}, "spec": spec},
        )

    async def sandbox(
        self, name: str, *, running: bool = True, conditions: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """A Sandbox of `TEMPLATE` with the mode and conditions its controller reported. Running, its
        incarnation is the Pod of its name -- so a test creates that Pod first -- or, with no such Pod,
        one that is gone."""
        await self._create(
            sandbox_resource.SANDBOXES_PLURAL,
            sandbox_resource.SANDBOX_KIND,
            name,
            {"template": TEMPLATE, "running": running},
        )
        status: dict[str, Any] = {
            "operatingMode": (OperatingMode.RUNNING if running else OperatingMode.SUSPENDED).value
        }
        if running:
            status["incarnation"] = {"name": name, "uid": await self._pod_uid(name)}
        if conditions is not None:
            status["conditions"] = conditions
        return await self.patch_status(sandbox_resource.SANDBOXES_PLURAL, name, status)

    async def _pod_uid(self, name: str) -> str:
        try:
            pod = await self.core_v1.read_namespaced_pod(name, self.name)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            return str(uuid4())
        return str(pod.metadata.uid)

    async def pod(
        self,
        name: str,
        *,
        phase: str,
        ready: bool,
        ip: str | None,
        waiting_reason: str | None = None,
        owner: dict[str, Any] | None = None,
    ) -> k8s_client.V1Pod:
        """A runner Pod as the kubelet reports it: running and ready, or held up by `waiting_reason`.
        An `owner` Sandbox controls it, and its Pod runs as that Sandbox's ServiceAccount."""
        await self.core_v1.create_namespaced_pod(
            self.name,
            k8s_client.V1Pod(
                metadata=k8s_client.V1ObjectMeta(
                    name=name,
                    owner_references=None
                    if owner is None
                    else [
                        k8s_client.V1OwnerReference(
                            api_version=sandbox_resource.SANDBOX_API_VERSION,
                            kind=sandbox_resource.SANDBOX_KIND,
                            name=owner["metadata"]["name"],
                            uid=owner["metadata"]["uid"],
                            controller=True,
                        )
                    ],
                ),
                spec=k8s_client.V1PodSpec(
                    containers=[k8s_client.V1Container(name="runner", image=RUNNER_IMAGE)],
                    node_name="test-node",
                    service_account_name=None if owner is None else owner["metadata"]["name"],
                ),
            ),
        )
        state = (
            {"running": {}}
            if waiting_reason is None
            else {"waiting": {"reason": waiting_reason, "message": f"{waiting_reason} on {name}"}}
        )
        return await self.core_v1.patch_namespaced_pod_status(
            name,
            self.name,
            {
                "status": {
                    "phase": phase,
                    **({"podIP": ip} if ip is not None else {}),
                    "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                    "containerStatuses": [
                        {
                            "name": "runner",
                            "image": RUNNER_IMAGE,
                            "imageID": "",
                            "ready": ready,
                            "restartCount": 0,
                            "state": state,
                        }
                    ],
                }
            },
        )

    async def runner(self, name: str) -> tuple[dict[str, Any], k8s_client.V1Pod]:
        """A Ready Sandbox and the running Pod it controls, as its controller leaves them."""
        owner = await self._create(
            sandbox_resource.SANDBOXES_PLURAL,
            sandbox_resource.SANDBOX_KIND,
            name,
            {"template": TEMPLATE, "running": True},
        )
        running = await self.pod(name, phase="Running", ready=True, ip="127.0.0.1", owner=owner)
        raw = await self.patch_status(
            sandbox_resource.SANDBOXES_PLURAL,
            name,
            {
                "operatingMode": OperatingMode.RUNNING.value,
                "incarnation": {"name": name, "uid": running.metadata.uid},
                "conditions": READY,
            },
        )
        return raw, running

    async def policy_set(self, name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        """A PolicySet holding these rules as given, each with its own `type`."""
        return await self._create(POLICY_SETS_PLURAL, "PolicySet", name, {"rules": rules})

    async def egress_policy_set(self, name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        """A PolicySet holding only `egress` rules, given without their `type`."""
        return await self.policy_set(name, [{"type": "egress", **rule} for rule in rules])

    async def action_policy_set(self, name: str, policies: list[dict[str, Any]]) -> dict[str, Any]:
        """A PolicySet holding one `action_auto_approve` rule per policy."""
        return await self.policy_set(name, [{"type": "action_auto_approve", "policy": policy} for policy in policies])

    async def egress_credential(
        self, name: str, *, source: dict[str, Any], description: str, targets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._create(
            CREDENTIALS_PLURAL,
            "EgressCredential",
            name,
            {"source": source, "description": description, "targets": targets},
        )

    async def policy_binding(
        self,
        name: str,
        *,
        subject: ServiceAccountRef,
        policy_sets: list[str],
        from_git: bool = True,
        expires_at: str | None = None,
        sandbox_controlled: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """A PolicyBinding; `from_git` stamps Flux's inventory label, `sandbox_controlled` names the
        Sandbox whose controller owns it, and `conditions` are the verdicts its readers have written."""
        created = await self._create(
            POLICY_BINDINGS_PLURAL,
            "PolicyBinding",
            name,
            {
                "subject": subject.model_dump(),
                "policySets": policy_sets,
                **({"expiresAt": expires_at} if expires_at is not None else {}),
            },
            labels={FLUX_KUSTOMIZATION_LABEL: "test-egress-kustomization"} if from_git else {},
            ownerReferences=[]
            if sandbox_controlled is None
            else [
                {
                    "apiVersion": sandbox_resource.SANDBOX_API_VERSION,
                    "kind": sandbox_resource.SANDBOX_KIND,
                    "name": sandbox_controlled,
                    "uid": str(uuid4()),
                    "controller": True,
                }
            ],
        )
        if conditions is None:
            return created
        return await self.patch_status(POLICY_BINDINGS_PLURAL, name, {"conditions": conditions})
