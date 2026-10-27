"""Agentplane's own `Sandbox` (agentplane/crds/manifests/crd-sandboxes.yaml) as its Python readers see
it; the Sandbox Controller (agentplane/sandbox_controller/) reconciles it.

Gotcha: agent-sandbox's `Sandbox`, which the cluster still installs for other workloads, has the same
kind and plural, so only the group tells them apart.
"""

from __future__ import annotations

from collections.abc import Set
from datetime import timedelta
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import durationpy
from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import Condition, ConditionStatus
from util.kubernetes import CustomObjectsClient

SANDBOXES_PLURAL = "sandboxes"
TEMPLATES_PLURAL = "sandboxtemplates"
SANDBOX_KIND = "Sandbox"
SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
# Set by the Runner Gateway on a deleting Sandbox once its runner sealed every Session and the
# ingress acknowledged each seal; the controller deletes nothing of the Sandbox before.
QUIESCED = "Quiesced"
# The Sandbox Controller's workload condition; its reasons say what the Sandbox is waiting on.
RUNNING = "Running"
# True while the Sandbox's workload runs and its runner is connected; its reason says why not.
READY = "Ready"
# On a Sandbox's Pod, or its VM, VMI and virt-launcher Pod: the Sandbox's name.
SANDBOX_LABEL = f"{GROUP}/sandbox"
# On a Sandbox: the app preset it was made from. Only the app reads it.
PRESET_LABEL = f"{GROUP}/preset"
# The reason a Sandbox's `Ready` and `Running` conditions carry while no Pod or VM runs.
STOPPED = "Stopped"
# A VM Sandbox's runner runs in its VMI's virt-launcher Pod, which the VMI controls.
VMI_API_VERSION = "kubevirt.io/v1"
VMI_KIND = "VirtualMachineInstance"


class OperatingMode(StrEnum):
    """Whether the Sandbox Controller runs the Sandbox."""

    RUNNING = "Running"
    SUSPENDED = "Suspended"


class SuspendedBy(StrEnum):
    """Why the Sandbox Controller reports a Sandbox as suspended."""

    IDLE = "Idle"
    REQUESTED = "Requested"


def _go_duration(value: object) -> object:
    """`metav1.Duration` is a Go duration string (`1h30m0s`) on the wire."""
    return durationpy.from_str(value) if isinstance(value, str) else value


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
    labels: dict[str, str] = Field(default_factory=dict)


class Spec(_Wire):
    template: str
    running: bool = False
    # Absent is the rollout-safe, disabled value; only an explicit true enables send wakes.
    wake_on_send: bool = False
    wake_generation: int = 0
    policy_sets: Set[str] = frozenset()
    bootstrap: str | None = None
    idle_suspend_after: Annotated[timedelta, BeforeValidator(_go_duration)] | None = None


class Incarnation(_Wire):
    """The Pod the Sandbox's runner runs in: its own Pod, or its VM's virt-launcher Pod."""

    name: str
    uid: str


class Status(_Wire):
    incarnation: Incarnation | None = None
    operating_mode: OperatingMode | None = None
    idle_suspend_at: AwareDatetime | None = None
    suspended_by: SuspendedBy | None = None
    suspended_at_wake_generation: int | None = None
    conditions: list[Condition] = Field(default_factory=list)


class Sandbox(_Wire):
    metadata: ObjectMeta
    spec: Spec
    status: Status = Field(default_factory=Status)

    @property
    def operating_mode(self) -> OperatingMode | None:
        """The controller's reported effective mode; absent until it first reconciles the Sandbox."""
        return self.status.operating_mode

    @property
    def sessions_sealed(self) -> bool:
        return any(c.type == QUIESCED and c.status is ConditionStatus.TRUE for c in self.status.conditions)

    @property
    def ready_condition(self) -> Condition | None:
        """The controller's reason for Ready or its current scheduling/start blocker."""
        return next((c for c in self.status.conditions if c.type == READY), None)

    @property
    def ready(self) -> bool:
        condition = self.ready_condition
        return condition is not None and condition.status is ConditionStatus.TRUE

    @property
    def suspended(self) -> bool:
        """Asked to stop and stopped: no Pod or VM runs, and the state volume is kept."""
        condition = self.ready_condition
        return self.operating_mode is OperatingMode.SUSPENDED and condition is not None and condition.reason == STOPPED

    def running_reason(self) -> str | None:
        return next((c.reason for c in self.status.conditions if c.type == RUNNING), None)

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
