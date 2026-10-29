"""`PolicySet` and `PolicyBinding` as their Python readers see them (agentplane/crds/manifests/).

Each reader parses only the rule types it needs. Expiry is not a reader's concern: the Policy
Controller deletes a binding once its `expiresAt` passes, so a binding that exists grants.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

POLICY_SETS_PLURAL = "policysets"
POLICY_BINDINGS_PLURAL = "policybindings"
# A metav1.Condition message is at most 32768 characters.
_REASON_LIMIT = 32768


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class OwnerReference(_Wire):
    kind: str
    controller: bool = False


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: str
    generation: int
    resource_version: str
    labels: dict[str, str] = Field(default_factory=dict)
    owner_references: list[OwnerReference] = Field(default_factory=list)


class ConditionStatus(StrEnum):
    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class Condition(_Wire):
    """A metav1.Condition."""

    type: str
    status: ConditionStatus
    reason: str
    message: str
    observed_generation: int | None = None
    last_transition_time: AwareDatetime


class BindingStatus(_Wire):
    conditions: list[Condition] = Field(default_factory=list)


class BindingSpec(_Wire):
    subject: ServiceAccountRef
    policy_sets: list[str] = Field(description="PolicySet names in the binding's namespace, in grant order.")
    expires_at: AwareDatetime | None = Field(
        default=None, description="Shown to operators only: the Policy Controller deletes the binding at this instant."
    )


class PolicyBinding(_Wire):
    metadata: ObjectMeta
    spec: BindingSpec
    status: BindingStatus = Field(default_factory=BindingStatus)


@dataclass(frozen=True)
class NumberedRule[R]:
    """One of a reader's rules, numbered by its position among all of the set's rules."""

    number: int
    rule: R


@dataclass(frozen=True)
class PolicySet[R]:
    """A set as one reader sees it: only the rules of the types that reader enforces."""

    name: str
    rules: tuple[NumberedRule[R], ...]
    generation: int | None = None  # None only on a set not read off the API server


@dataclass(frozen=True)
class RefusedPolicySet:
    """A set one of whose rules this reader enforces does not parse: it grants this reader nothing."""

    name: str
    generation: int
    reason: str


class _RawSpec(_Wire):
    rules: list[dict[str, Any]]


class _RawPolicySet(_Wire):
    metadata: ObjectMeta
    spec: _RawSpec


def parse_policy_set[R](
    raw: object, *, parse_rule: Callable[[dict[str, Any]], R | None]
) -> PolicySet[R] | RefusedPolicySet:
    """Keeps the rules `parse_rule` parses; it returns None for another reader's rule type. A rule
    of this reader's that does not parse refuses the whole set, which then grants this reader nothing
    rather than part of what its author meant."""
    policy_set = _RawPolicySet.model_validate(raw)
    metadata = policy_set.metadata
    rules: list[NumberedRule[R]] = []
    for number, raw_rule in enumerate(policy_set.spec.rules):
        try:
            rule = parse_rule(raw_rule)
        except (ValidationError, ValueError) as error:
            logger.warning("PolicySet %s refused: rules[%d]: %s", metadata.name, number, error)
            return RefusedPolicySet(
                name=metadata.name, generation=metadata.generation, reason=f"rules[{number}]: {error}"[:_REASON_LIMIT]
            )
        if rule is not None:
            rules.append(NumberedRule(number=number, rule=rule))
    return PolicySet(name=metadata.name, rules=tuple(rules), generation=metadata.generation)
