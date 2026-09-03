"""Typed access to kubernetes_asyncio's generated dynamic custom-object API.

`CustomObjectsApi` is generated from the OpenAPI spec, and its stub for
`patch_namespaced_custom_object` omits the `_content_type` keyword the runtime accepts. Casting the
generated client to this Protocol is how a caller sends `application/json-patch+json` — a merge
patch cannot carry the `test` op a resourceVersion guard relies on — without a per-call
`# type: ignore`. The rest of the surface is declared here so one cast covers a caller's whole use
of the dynamic client rather than one per method.
"""

from __future__ import annotations

from typing import Any, Protocol

from kubernetes_asyncio import client as k8s_client


class CustomObjectsClient(Protocol):
    """Typed subset of the generated dynamic custom-object API."""

    async def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        *,
        label_selector: str = ...,
        limit: int = ...,
        _continue: str = ...,
    ) -> dict[str, Any]: ...

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]: ...

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object: ...

    async def patch_namespaced_custom_object_status(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object: ...

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, *, body: k8s_client.V1DeleteOptions
    ) -> object: ...
