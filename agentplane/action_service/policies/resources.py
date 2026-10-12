"""The policy resources the Action Service reads off the API server, parsed once at the boundary.

`ActionPolicySet` and `ActionPolicyBinding` are Agentplane's own kinds (group
`agentplane.allegedly.works`, `v1alpha1`; the CRDs live in `agentplane/crds`). The generic
`PolicySet` carries the same policies as `action_auto_approve` rules, which `parse_action_rule` reads
for `agentplane/policy_sets.py`. A
caller ServiceAccount is an ordinary ServiceAccount carrying `CALLER_LABEL`, which is what admits
it here at all -- a Pod running as it and a Connection acting as it alike. The operator-authored
`spec` is parsed strictly, so a typo or an unknown policy kind is refused here rather than
silently granting nothing; such an object is kept as `InvalidResource`, which the informer reports
in the object's `Ready` condition. Server-stamped metadata and status are read only as far as the
service needs them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from agentplane.action_service.models import NamespacedName
from agentplane.action_service.policies.kind import Spec
from agentplane.action_service.policies.registry import Policy
from agentplane.crd_group import GROUP
from agentplane.subjects import ServiceAccountRef

POLICY_SETS_PLURAL = "actionpolicysets"
BINDINGS_PLURAL = "actionpolicybindings"
SERVICE_ACCOUNTS_PLURAL = "serviceaccounts"
CALLER_LABEL = f"{GROUP}/use-action-service"
CALLER_LABEL_SELECTOR = f"{CALLER_LABEL}=true"
READY_CONDITION = "Ready"
# On a PolicyBinding, beside the other readers' conditions; written by server-side apply under this
# service's field manager.
ACTIONS_READY_CONDITION = "ActionsReady"
ACTIONS_FIELD_MANAGER = "agentplane-action-service"
ACTION_RULE_TYPE = "action_auto_approve"
# metav1.Condition.message is capped by the CRD schema; a pydantic report for a large object can be longer.
_MESSAGE_LIMIT = 32768


class _Wire(BaseModel):
    """Server-stamped envelope fields, read off the API server; constructed by field name in tests."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: str
    labels: dict[str, str] = Field(
        default_factory=dict, description="Read through to the operator view; the service decides nothing from them."
    )
    generation: int = Field(description="Bumped by the API server on every spec change; a status write leaves it.")
    resource_version: str = Field(
        alias="resourceVersion", description="Bumped on every write, status included; what a Decision records."
    )


class _Namespaced(_Wire):
    """A namespaced object as read off the API server; `namespaced_name` is its key in the index."""

    metadata: ObjectMeta

    @property
    def namespaced_name(self) -> NamespacedName:
        return NamespacedName(self.metadata.namespace, self.metadata.name)


class Condition(_Wire):
    """A metav1.Condition as this service reads and writes it."""

    type: str
    status: Literal["True", "False", "Unknown"]
    reason: str
    message: str
    observed_generation: int | None = Field(default=None, alias="observedGeneration")
    last_transition_time: AwareDatetime = Field(alias="lastTransitionTime")


class Status(_Wire):
    conditions: list[Condition] = Field(default_factory=list)

    def ready(self) -> Condition | None:
        return next((condition for condition in self.conditions if condition.type == READY_CONDITION), None)


class PolicySetSpec(Spec):
    auto_approve_if: list[Policy] = Field(
        default_factory=list, alias="autoApproveIf", description="A request matching any policy here is auto-approved."
    )


class ActionPolicySet(_Namespaced):
    spec: PolicySetSpec
    status: Status = Field(default_factory=Status)


class BindingSpec(Spec):
    subject: ServiceAccountRef
    policy_sets: list[str] = Field(
        alias="policySets", min_length=1, description="ActionPolicySet names in the binding's namespace."
    )
    expires_at: AwareDatetime | None = Field(
        default=None, alias="expiresAt", description="After this instant the binding contributes nothing."
    )


class ActionPolicyBinding(_Namespaced):
    spec: BindingSpec
    status: Status = Field(default_factory=Status)


class AutoApproveRule(Spec):
    """A PolicySet rule this service reads: a request `policy` matches is auto-approved."""

    type: Literal["action_auto_approve"]
    policy: Policy


def parse_action_rule(raw: Mapping[str, JsonValue]) -> Policy | None:
    """A PolicySet rule's auto-approve policy, or None for another reader's rule type."""
    if raw["type"] != ACTION_RULE_TYPE:
        return None
    return AutoApproveRule.model_validate(raw).policy


@dataclass(frozen=True)
class InvalidResource:
    """An object whose spec this service refused; it contributes nothing and reports why in Ready."""

    metadata: ObjectMeta
    status: Status
    message: str

    @property
    def namespaced_name(self) -> NamespacedName:
        return NamespacedName(self.metadata.namespace, self.metadata.name)


def _report(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'spec'}: {item['msg']}" for item in error.errors()
    )[:_MESSAGE_LIMIT]


def parse_policy_set(raw: dict[str, Any]) -> ActionPolicySet | InvalidResource:
    try:
        return ActionPolicySet.model_validate(raw)
    except ValidationError as error:
        return InvalidResource(
            metadata=ObjectMeta.model_validate(raw["metadata"]),
            status=Status.model_validate(raw.get("status", {})),
            message=_report(error),
        )


def parse_binding(raw: dict[str, Any]) -> ActionPolicyBinding | InvalidResource:
    try:
        return ActionPolicyBinding.model_validate(raw)
    except ValidationError as error:
        return InvalidResource(
            metadata=ObjectMeta.model_validate(raw["metadata"]),
            status=Status.model_validate(raw.get("status", {})),
            message=_report(error),
        )
