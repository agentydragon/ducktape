"""Idempotent creation of a UID-owned initial grant, never adoption of a conflicting object."""

from typing import Any

from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.errors import InventoryError
from util.kubernetes import CustomObjectsClient


class BindingConflictError(InventoryError):
    """The deterministic initial grant name is occupied by different ownership or permissions."""


async def create_binding(
    custom: CustomObjectsClient, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await custom.create_namespaced_custom_object(group, version, namespace, plural, body)
    except k8s_client.ApiException as error:
        name = body["metadata"].get("name")
        if error.status != 409 or not name:
            raise
    existing = await custom.get_namespaced_custom_object(group, version, namespace, plural, name)
    metadata = existing.get("metadata", {})
    if (
        existing.get("spec") != body["spec"]
        or metadata.get("ownerReferences") != body["metadata"]["ownerReferences"]
        or any(
            metadata.get("labels", {}).get(key) != value for key, value in body["metadata"].get("labels", {}).items()
        )
        or metadata.get("deletionTimestamp") is not None
    ):
        raise BindingConflictError(f"initial {plural} binding {name} conflicts with the recorded provisioning intent")
    return existing
