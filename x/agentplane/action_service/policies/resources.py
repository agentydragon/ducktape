"""The policy resources the Action Service reads off the API server, parsed once at the boundary.

`ActionPolicySet` and `ActionPolicyBinding` are Agentplane's own kinds (group
`agentplane.allegedly.works`, `v1alpha1`; the CRDs live in `cluster/k8s/agentplane-crds`). A
caller ServiceAccount is an ordinary ServiceAccount carrying `CALLER_LABEL`. The operator-authored
`spec` is parsed strictly, so a typo or an unknown policy kind is refused here rather than
silently granting nothing; such an object is kept as `InvalidResource`, which the informer reports
in the object's `Ready` condition. Server-stamped metadata and status are read only as far as the
service needs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError

from x.agentplane.action_service.models import ServiceAccountRef
from x.agentplane.action_service.policies.kind import Spec
from x.agentplane.action_service.policies.registry import Policy

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
POLICY_SETS_PLURAL = "actionpolicysets"
BINDINGS_PLURAL = "actionpolicybindings"
SERVICE_ACCOUNTS_PLURAL = "serviceaccounts"
CALLER_LABEL = "agentplane.allegedly.works/action-caller"
CALLER_LABEL_SELECTOR = f"{CALLER_LABEL}=true"
READY_CONDITION = "Ready"
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
    auto_deny_if: list[Policy] = Field(
        default_factory=list, alias="autoDenyIf", description="A request matching any policy here is auto-denied."
    )
    auto_deny_unless: list[Policy] = Field(
        default_factory=list,
        alias="autoDenyUnless",
        description="A request matching none of the policies here is auto-denied.",
    )


class ActionPolicySet(_Wire):
    metadata: ObjectMeta
    spec: PolicySetSpec
    status: Status = Field(default_factory=Status)


class SandboxRef(Spec):
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1, description="Pins the live Sandbox; a binding whose Sandbox is gone is inert.")


class ServiceAccountSubject(Spec):
    service_account: ServiceAccountRef = Field(alias="serviceAccount")


class SandboxSubject(Spec):
    sandbox: SandboxRef


def _subject_kind(value: Any) -> str | None:
    """Which one-key form the subject takes; both keys at once fails as an extra field on the chosen one."""
    if isinstance(value, ServiceAccountSubject):
        return "serviceAccount"
    if isinstance(value, SandboxSubject):
        return "sandbox"
    if isinstance(value, dict):
        for key in ("serviceAccount", "service_account"):
            if key in value:
                return "serviceAccount"
        if "sandbox" in value:
            return "sandbox"
    return None


Subject = Annotated[
    Annotated[ServiceAccountSubject, Tag("serviceAccount")] | Annotated[SandboxSubject, Tag("sandbox")],
    Discriminator(_subject_kind),
]


class BindingSpec(Spec):
    subject: Subject
    policy_sets: list[str] = Field(
        alias="policySets", min_length=1, description="ActionPolicySet names in the binding's namespace."
    )
    expires_at: AwareDatetime | None = Field(
        default=None, alias="expiresAt", description="After this instant the binding contributes nothing."
    )


class ActionPolicyBinding(_Wire):
    metadata: ObjectMeta
    spec: BindingSpec
    status: Status = Field(default_factory=Status)


@dataclass(frozen=True)
class InvalidResource:
    """An object whose spec this service refused; it contributes nothing and reports why in Ready."""

    metadata: ObjectMeta
    status: Status
    message: str


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
