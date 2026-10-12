"""`PolicySet` and `PolicyBinding` as their readers parse them off the API server.

Both are Agentplane's own kinds (group `agentplane.allegedly.works`, `v1alpha1`; the CRDs live in
`agentplane/crds`). A set carries typed rules for several services, and a binding attaches sets to
one ServiceAccount; each reader enforces only its own rule types and reports on them in its own
status condition, applied under its own field manager so readers never overwrite each other's.

The operator-authored `spec` is parsed strictly, so a typo is refused rather than silently granting
nothing. Every reader parses the whole set, so one bad rule invalidates the set for all of them: such
an object is kept as `InvalidResource`, and each reader reports why in its own condition.
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from agentplane.subjects import ServiceAccountRef

POLICY_SETS_PLURAL = "policysets"
POLICY_BINDINGS_PLURAL = "policybindings"
# metav1.Condition.message is capped by the CRD schema; a pydantic report for a large object can be longer.
MESSAGE_LIMIT = 32768


class _Wire(BaseModel):
    """Server-stamped envelope fields: read only as far as a reader needs them."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class _Spec(BaseModel):
    """Operator-authored fields: an unknown key is a mistake, never ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: str
    generation: int = Field(description="Bumped by the API server on every spec change; a status write leaves it.")


class Condition(_Wire):
    """A metav1.Condition as readers read and apply it."""

    type: str
    status: Literal["True", "False", "Unknown"]
    reason: str
    message: str
    observed_generation: int | None = Field(default=None, alias="observedGeneration")
    last_transition_time: AwareDatetime = Field(alias="lastTransitionTime")


class Status(_Wire):
    conditions: list[Condition] = Field(default_factory=list)

    def condition(self, type: str) -> Condition | None:
        return next((condition for condition in self.conditions if condition.type == type), None)


class RoleRef(_Spec):
    kind: Literal["Role", "ClusterRole"]
    name: str = Field(min_length=1)


class KubernetesRoleBindingRule(_Spec):
    type: Literal["kubernetes_role_binding"]
    role: RoleRef
    namespaces: Set[str] = Field(min_length=1)


class KubernetesClusterRoleBindingRule(_Spec):
    type: Literal["kubernetes_cluster_role_binding"]
    cluster_role: str = Field(alias="clusterRole", min_length=1)


Rule = Annotated[KubernetesRoleBindingRule | KubernetesClusterRoleBindingRule, Field(discriminator="type")]


class PolicySetSpec(_Spec):
    rules: list[Rule] = Field(min_length=1)


class PolicySet(_Wire):
    metadata: ObjectMeta
    spec: PolicySetSpec
    status: Status = Field(default_factory=Status)


class PolicyBindingSpec(_Spec):
    subject: ServiceAccountRef
    policy_sets: list[str] = Field(alias="policySets", min_length=1, description="PolicySet names in its namespace.")
    expires_at: AwareDatetime | None = Field(
        default=None, alias="expiresAt", description="After this instant the binding contributes nothing."
    )


class PolicyBinding(_Wire):
    metadata: ObjectMeta
    spec: PolicyBindingSpec
    status: Status = Field(default_factory=Status)


@dataclass(frozen=True)
class InvalidResource:
    """An object whose spec failed to parse; it contributes nothing, and its reader reports why."""

    metadata: ObjectMeta
    status: Status
    message: str


def _invalid(raw: dict[str, Any], error: ValidationError) -> InvalidResource:
    return InvalidResource(
        metadata=ObjectMeta.model_validate(raw["metadata"]),
        status=Status.model_validate(raw.get("status", {})),
        message="; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'spec'}: {item['msg']}" for item in error.errors()
        )[:MESSAGE_LIMIT],
    )


def parse_policy_set(raw: dict[str, Any]) -> PolicySet | InvalidResource:
    try:
        return PolicySet.model_validate(raw)
    except ValidationError as error:
        return _invalid(raw, error)


def parse_policy_binding(raw: dict[str, Any]) -> PolicyBinding | InvalidResource:
    try:
        return PolicyBinding.model_validate(raw)
    except ValidationError as error:
        return _invalid(raw, error)
