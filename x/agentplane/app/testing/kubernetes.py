"""An in-memory Kubernetes for the inventory tests: dynamic custom objects and Pods, no cluster.

The fakes keep the objects the real API would (a merge patch applies, a create stamps a
creationTimestamp), so a test can assert the state the inventory reads back, not only the calls it
made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client

from x.agentplane.app.egress import GRANTED_BY_LABEL
from x.agentplane.app.inventory import MANAGED_LABEL

NAMESPACE = "agentplane-test"
TEMPLATE = "agentplane-test-runner"

# What the test template carries, and what every Sandbox the inventory creates must copy.
POD_TEMPLATE: dict[str, Any] = {
    "metadata": {"labels": {"app.kubernetes.io/name": "agentplane-test-runner"}},
    "spec": {"containers": [{"name": "runner", "image": "registry.test/agentplane-runner:test"}]},
}
VOLUME_CLAIM_TEMPLATES: list[dict[str, Any]] = [
    {"metadata": {"name": "state"}, "spec": {"resources": {"requests": {"storage": "1Gi"}}}}
]


def merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """RFC 7386 merge patch in place: nested objects recurse, `None` deletes."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_patch(target[key], value)
        else:
            target[key] = value


class FakeCustomObjectsApi:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {
            ("sandboxtemplates", TEMPLATE): {
                "metadata": {"name": TEMPLATE, "creationTimestamp": "2026-09-01T11:00:00Z"},
                "spec": {
                    "podTemplate": POD_TEMPLATE,
                    "volumeClaimTemplatesPolicy": "Overrides",
                    "volumeClaimTemplates": VOLUME_CLAIM_TEMPLATES,
                },
            }
        }
        self.patches: list[tuple[str, str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []

    async def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, *, label_selector: str = ""
    ) -> dict[str, Any]:
        del group, version
        assert namespace == NAMESPACE
        wanted = dict(item.split("=", 1) for item in label_selector.split(",") if item)
        items = [
            obj
            for (kind, _), obj in self.objects.items()
            if kind == plural and all(obj["metadata"].get("labels", {}).get(k) == v for k, v in wanted.items())
        ]
        return {"items": items}

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del group, version
        assert namespace == NAMESPACE
        key = (plural, body["metadata"]["name"])
        if key in self.objects:
            raise k8s_client.ApiException(status=409)
        stored = {
            **body,
            "metadata": {**body["metadata"], "uid": str(uuid4()), "creationTimestamp": "2026-09-02T10:00:00Z"},
        }
        self.objects[key] = stored
        return stored

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        del group, version
        assert namespace == NAMESPACE
        try:
            return self.objects[(plural, name)]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        del group, version
        assert namespace == NAMESPACE
        assert _content_type == "application/merge-patch+json"
        assert isinstance(body, dict)
        target = await self.get_namespaced_custom_object("", "", namespace, plural, name)
        merge_patch(target, body)
        self.patches.append((plural, name, body))
        return target

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, *, body: k8s_client.V1DeleteOptions
    ) -> object:
        del group, version, body
        assert namespace == NAMESPACE
        await self.get_namespaced_custom_object("", "", namespace, plural, name)
        del self.objects[(plural, name)]
        self.deleted.append((plural, name))
        return {}


class FakeCoreV1Api:
    def __init__(self) -> None:
        self.pods: dict[str, k8s_client.V1Pod] = {}

    async def list_namespaced_pod(self, namespace: str) -> k8s_client.V1PodList:
        assert namespace == NAMESPACE
        return k8s_client.V1PodList(items=list(self.pods.values()))

    async def read_namespaced_pod(self, name: str, namespace: str) -> k8s_client.V1Pod:
        assert namespace == NAMESPACE
        try:
            return self.pods[name]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None


def sandbox(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    operating_mode: str = "Running",
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "uid": str(uuid4()),
            "labels": {MANAGED_LABEL: "true", **(labels or {})},
            "creationTimestamp": "2026-09-01T12:00:00Z",
        },
        "spec": {"podTemplate": POD_TEMPLATE, "operatingMode": operating_mode},
        **({"status": status} if status is not None else {}),
    }


def egress_policy(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "uid": str(uuid4()), "creationTimestamp": "2026-09-01T11:30:00Z"},
        "spec": {"rules": rules},
    }


def egress_binding(
    name: str,
    *,
    subjects: list[dict[str, Any]],
    policies: list[str],
    granted_by: str | None = "flux",
    expires_at: str | None = None,
    active: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """A binding as the API server holds it; `active` is (status, reason, message) of the proxy's condition."""
    return {
        "metadata": {
            "name": name,
            "uid": str(uuid4()),
            "labels": {GRANTED_BY_LABEL: granted_by} if granted_by is not None else {},
            "creationTimestamp": "2026-09-01T11:45:00Z",
        },
        "spec": {
            "subjects": subjects,
            "policies": policies,
            **({"expiresAt": expires_at} if expires_at is not None else {}),
        },
        **(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Active",
                            "status": active[0],
                            "reason": active[1],
                            "message": active[2],
                            "lastTransitionTime": "2026-09-01T11:46:00Z",
                        }
                    ]
                }
            }
            if active is not None
            else {}
        ),
    }


def pod(name: str, *, phase: str, ready: bool, ip: str | None, waiting_reason: str | None = None) -> k8s_client.V1Pod:
    """A runner Pod as the kubelet reports it: running and ready, or held up by `waiting_reason`."""
    state = (
        k8s_client.V1ContainerState(running=k8s_client.V1ContainerStateRunning())
        if waiting_reason is None
        else k8s_client.V1ContainerState(
            waiting=k8s_client.V1ContainerStateWaiting(reason=waiting_reason, message=f"{waiting_reason} on {name}")
        )
    )
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name=name, creation_timestamp=datetime(2026, 9, 1, 12, 0, 10, tzinfo=UTC)),
        spec=k8s_client.V1PodSpec(containers=[k8s_client.V1Container(name="runner")], node_name="test-node"),
        status=k8s_client.V1PodStatus(
            phase=phase,
            pod_ip=ip,
            conditions=[k8s_client.V1PodCondition(type="Ready", status="True" if ready else "False")],
            container_statuses=[
                k8s_client.V1ContainerStatus(
                    name="runner",
                    image="registry.test/agentplane-runner:test",
                    image_id="",
                    ready=ready,
                    restart_count=0,
                    state=state,
                )
            ],
        ),
    )


class FakeAuthenticationV1Api:
    """TokenReview over a table of tokens, answering as the API server does for an unknown one.

    A token whose audiences do not cover what was asked still comes back authenticated, with the
    intersection: the API server would refuse it outright, and answering the softer way is what
    exercises the reviewer's own audience check.
    """

    def __init__(self) -> None:
        self.tokens: dict[str, tuple[str, list[str]]] = {}

    def issue(self, token: str, *, username: str, audiences: list[str]) -> None:
        self.tokens[token] = (username, audiences)

    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        held = self.tokens.get(body.spec.token)
        if held is None:
            return k8s_client.V1TokenReview(
                spec=body.spec,
                status=k8s_client.V1TokenReviewStatus(authenticated=False, error="[invalid bearer token]"),
            )
        username, audiences = held
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=True,
                audiences=[audience for audience in audiences if audience in (body.spec.audiences or [])],
                user=k8s_client.V1UserInfo(username=username),
            ),
        )
