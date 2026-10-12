"""Agentplane's own `Sandbox` (agentplane/crds/manifests/crd-sandboxes.yaml) as its Python readers see
it; the Sandbox Controller (agentplane/sandbox_controller/) reconciles it.

Gotcha: agent-sandbox's `Sandbox` has the same kind and plural, so only the group tells them apart.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import Condition, ConditionStatus
from util.kubernetes import CustomObjectsClient

SANDBOXES_PLURAL = "sandboxes"
SANDBOX_KIND = "Sandbox"
SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
# Set by the Runner Gateway on a deleting Sandbox once its runner sealed every Session and the
# ingress acknowledged each seal; the controller deletes nothing of the Sandbox before.
SESSIONS_SEALED = "SessionsSealed"
_APPLY_PATCH = "application/apply-patch+yaml"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: UUID
    generation: int
    deletion_timestamp: AwareDatetime | None = None


class Status(_Wire):
    conditions: list[Condition] = Field(default_factory=list)


class Sandbox(_Wire):
    metadata: ObjectMeta
    status: Status = Field(default_factory=Status)

    @property
    def sessions_sealed(self) -> bool:
        return any(c.type == SESSIONS_SEALED and c.status is ConditionStatus.TRUE for c in self.status.conditions)


async def read_sandbox(custom_objects: CustomObjectsClient, namespace: str, name: str) -> Sandbox | None:
    try:
        raw = await custom_objects.get_namespaced_custom_object(GROUP, VERSION, namespace, SANDBOXES_PLURAL, name)
    except k8s_client.ApiException as error:
        if error.status == 404:
            return None
        raise
    return Sandbox.model_validate(raw)


async def report_sessions_sealed(
    custom_objects: CustomObjectsClient, sandbox: Sandbox, *, message: str, field_manager: str, now: datetime
) -> None:
    """Apply `SessionsSealed` server-side under `field_manager`, so the controller's conditions stay."""
    metadata = sandbox.metadata
    condition = Condition(
        type=SESSIONS_SEALED,
        status=ConditionStatus.TRUE,
        reason="Sealed",
        message=message,
        observed_generation=metadata.generation,
        last_transition_time=now,
    )
    await custom_objects.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        metadata.namespace,
        SANDBOXES_PLURAL,
        metadata.name,
        {
            "apiVersion": SANDBOX_API_VERSION,
            "kind": SANDBOX_KIND,
            # A precondition: a recreated Sandbox of the name is refused, not marked sealed.
            "metadata": {"name": metadata.name, "namespace": metadata.namespace, "uid": str(metadata.uid)},
            "status": {"conditions": [condition.model_dump(mode="json", by_alias=True, exclude_none=True)]},
        },
        _content_type=_APPLY_PATCH,
        field_manager=field_manager,
        force=True,
    )
