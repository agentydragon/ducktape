"""An in-memory Kubernetes for the inventory tests: dynamic custom objects and Pods, no cluster.

The fakes keep the objects the real API would (a merge patch applies, a create stamps a
creationTimestamp), so a test can assert the state the inventory reads back, not only the calls it
made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client as k8s_client

from x.agentplane.app.inventory import MANAGED_LABEL, MODEL_ANNOTATION, PROVIDER_LABEL, Provider

NAMESPACE = "agentplane-test"
WARM_POOL = "agentplane-test-pool"


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
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
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
        stored = {**body, "metadata": {**body["metadata"], "creationTimestamp": "2026-09-02T10:00:00Z"}}
        self.objects[(plural, stored["metadata"]["name"])] = stored
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


def claim(
    name: str,
    *,
    provider: Provider = Provider.CLAUDE,
    model: str = "test-model",
    labels: dict[str, str] | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {MANAGED_LABEL: "true", PROVIDER_LABEL: provider, **(labels or {})},
            "annotations": {MODEL_ANNOTATION: model},
            "creationTimestamp": "2026-09-01T12:00:00Z",
        },
        "spec": {"warmPoolRef": {"name": WARM_POOL}},
        **({"status": status} if status is not None else {}),
    }


def sandbox(name: str, *, operating_mode: str = "Running") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "creationTimestamp": "2026-09-01T12:00:05Z"},
        "spec": {"operatingMode": operating_mode},
    }


def pod(name: str, *, phase: str, ready: bool, ip: str | None) -> k8s_client.V1Pod:
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name=name, creation_timestamp=datetime(2026, 9, 1, 12, 0, 10, tzinfo=UTC)),
        status=k8s_client.V1PodStatus(
            phase=phase,
            pod_ip=ip,
            conditions=[k8s_client.V1PodCondition(type="Ready", status="True" if ready else "False")],
        ),
    )
