"""Agentplane's own `Sandbox` (agentplane/crds/manifests/crd-sandboxes.yaml) as its Python readers see
it; the Sandbox Controller (agentplane/sandbox_controller/) reconciles it.

Gotcha: agent-sandbox's `Sandbox`, which the cluster still installs for other workloads, has the same
kind and plural, so only the group tells them apart.
"""

from __future__ import annotations

from collections.abc import Set
from enum import StrEnum
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import Condition, ConditionStatus
from util.kubernetes import CustomObjectsClient

SANDBOXES_PLURAL = "sandboxes"
TEMPLATES_PLURAL = "sandboxtemplates"
SANDBOX_KIND = "Sandbox"
SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
# Set by the Sandbox Controller on a deleting Sandbox once its runner sealed every Session and the
# ingress acknowledged each seal; the controller deletes nothing of the Sandbox before.
SESSIONS_SEALED = "SessionsSealed"
# On a Sandbox's Pod, or its VM, VMI and virt-launcher Pod: the Sandbox's name.
SANDBOX_LABEL = f"{GROUP}/sandbox"
# A VM Sandbox's runner runs in its VMI's virt-launcher Pod, which the VMI controls.
VMI_API_VERSION = "kubevirt.io/v1"
VMI_KIND = "VirtualMachineInstance"


class OperatingMode(StrEnum):
    """The Sandbox API's `operating_mode`, from `spec.running`."""

    RUNNING = "Running"
    SUSPENDED = "Suspended"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: UUID
    generation: int
    resource_version: str
    creation_timestamp: AwareDatetime
    deletion_timestamp: AwareDatetime | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class SessionDefaults(_Wire):
    harness: str | None = None
    model: str | None = None
    cwd: str | None = None
    reasoning_effort: str | None = None
    instructions: str | None = None
    setup_script: str | None = None


class Spec(_Wire):
    template: str
    running: bool = False
    policy_sets: Set[str] = frozenset()
    session_defaults: SessionDefaults | None = None
    bootstrap: str | None = None


class Incarnation(_Wire):
    """The Pod the Sandbox's runner runs in: its own Pod, or its VM's virt-launcher Pod."""

    name: str
    uid: str


class Status(_Wire):
    incarnation: Incarnation | None = None
    conditions: list[Condition] = Field(default_factory=list)


class Sandbox(_Wire):
    metadata: ObjectMeta
    spec: Spec
    status: Status = Field(default_factory=Status)

    @property
    def sessions_sealed(self) -> bool:
        return any(c.type == SESSIONS_SEALED and c.status is ConditionStatus.TRUE for c in self.status.conditions)

    def runs_in(self, pod: k8s_client.V1Pod) -> bool:
        """Whether `pod` is the incarnation the Sandbox Controller reports, the same object and not
        one recreated under its name. Only the controller writes it, so this holds for a VM's
        virt-launcher Pod, which the Sandbox does not control."""
        incarnation = self.status.incarnation
        return incarnation is not None and pod.metadata is not None and pod.metadata.uid == incarnation.uid


async def read_sandbox(custom_objects: CustomObjectsClient, namespace: str, name: str) -> Sandbox | None:
    try:
        raw = await custom_objects.get_namespaced_custom_object(GROUP, VERSION, namespace, SANDBOXES_PLURAL, name)
    except k8s_client.ApiException as error:
        if error.status == 404:
            return None
        raise
    return Sandbox.model_validate(raw)
