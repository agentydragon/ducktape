"""Write Agentplane custom resources to a real Kubernetes API server and see what admission does.

The schema and CEL rules are the API server's, so a refused write is a 422 and stores nothing.
"""

from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from kubernetes_asyncio import client as k8s_client

from agentplane.crd_group import GROUP, VERSION
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver


@dataclass(frozen=True)
class CrdAdmission:
    """One kind's resources in a namespace. Each `rejects_*` asserts that admission refuses that write with a 422."""

    api: k8s_client.CustomObjectsApi
    namespace: str
    plural: str
    kind: str

    async def create(self, name: str, spec: dict[str, Any], **metadata: Any) -> dict[str, Any]:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": self.kind,
            "metadata": {"name": name, **metadata},
            "spec": spec,
        }
        return cast(
            dict[str, Any],
            await self.api.create_namespaced_custom_object(GROUP, VERSION, self.namespace, self.plural, body),
        )

    async def replace_spec(self, resource: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.api.replace_namespaced_custom_object(
                GROUP, VERSION, self.namespace, self.plural, resource["metadata"]["name"], {**resource, "spec": spec}
            ),
        )

    async def replace_status(self, resource: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.api.replace_namespaced_custom_object_status(
                GROUP,
                VERSION,
                self.namespace,
                self.plural,
                resource["metadata"]["name"],
                {**resource, "status": status},
            ),
        )

    async def rejects_create(self, spec: dict[str, Any], name: str = "test-rejected") -> None:
        await _rejected(self.create(name, spec), f"{name} {spec}")

    async def rejects_spec(self, resource: dict[str, Any], spec: dict[str, Any]) -> None:
        await _rejected(self.replace_spec(resource, spec), spec)

    async def rejects_status(self, resource: dict[str, Any], status: dict[str, Any]) -> None:
        await _rejected(self.replace_status(resource, status), status)


@dataclass(frozen=True)
class CrdNamespace:
    api: k8s_client.CustomObjectsApi
    namespace: str

    def kind(self, plural: str, kind: str) -> CrdAdmission:
        return CrdAdmission(self.api, self.namespace, plural, kind)


@asynccontextmanager
async def crd_namespace(namespace: str) -> AsyncIterator[CrdNamespace]:
    """A namespace on an API server of its own, which serves every Agentplane CRD."""
    async with kubernetes_apiserver() as server:
        await k8s_client.CoreV1Api(server.api).create_namespace(
            k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace))
        )
        yield CrdNamespace(k8s_client.CustomObjectsApi(server.api), namespace)


async def _rejected(write: Awaitable[object], what: object) -> None:
    with pytest.raises(k8s_client.ApiException) as rejected:
        await write
    assert rejected.value.status == 422, f"{what}: {rejected.value.body!r}"
