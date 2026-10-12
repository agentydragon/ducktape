"""`PolicySet` and `PolicyBinding` as their Python readers see them (agentplane/crds/manifests/).

Each reader parses only the rule types it enforces, resolves a subject's bindings to sets, and keeps
a status condition of its own on every binding. Expiry is not a reader's concern: the Policy
Controller deletes a binding once its `expiresAt` passes, so a binding that exists grants.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from agentplane.crd_group import GROUP, VERSION
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)

POLICY_SETS_PLURAL = "policysets"
POLICY_BINDINGS_PLURAL = "policybindings"
# A reason the Policy Controller reports too, so one missing set reads the same in every condition.
POLICY_SET_UNAVAILABLE = "PolicySetUnavailable"
_APPLY_PATCH = "application/apply-patch+yaml"
# A metav1.Condition message is at most 32768 characters.
_REASON_LIMIT = 32768


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: str
    generation: int
    resource_version: str
    labels: dict[str, str] = Field(default_factory=dict)


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

    def says_same_as(self, other: Condition | None) -> bool:
        """Equal but for when it last changed, which is what a writer compares before writing again."""
        return other is not None and (self.status, self.reason, self.message, self.observed_generation) == (
            other.status,
            other.reason,
            other.message,
            other.observed_generation,
        )


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

    def condition(self, condition_type: str) -> Condition | None:
        return next((condition for condition in self.status.conditions if condition.type == condition_type), None)


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


@dataclass(frozen=True)
class BindingResolution[R]:
    """A binding and the sets it names, as they stand: a missing or refused set takes nothing from the
    others."""

    binding: PolicyBinding
    sets: tuple[PolicySet[R], ...]
    missing: tuple[str, ...]
    refused: tuple[RefusedPolicySet, ...]


def resolve_binding[R](
    sets: Mapping[str, PolicySet[R] | RefusedPolicySet], binding: PolicyBinding
) -> BindingResolution[R]:
    names = binding.spec.policy_sets
    found = [sets[name] for name in names if name in sets]
    return BindingResolution(
        binding=binding,
        sets=tuple(policy_set for policy_set in found if isinstance(policy_set, PolicySet)),
        missing=tuple(name for name in names if name not in sets),
        refused=tuple(policy_set for policy_set in found if isinstance(policy_set, RefusedPolicySet)),
    )


def unavailable_message(resolution: BindingResolution[Any]) -> str:
    """Which named sets grant nothing, for a reader's condition."""
    return "; ".join(
        [
            *([f"missing PolicySets: {', '.join(resolution.missing)}"] if resolution.missing else []),
            *(f"PolicySet {refused.name} refused: {refused.reason}" for refused in resolution.refused),
        ]
    )[:_REASON_LIMIT]


def subject_bindings[R](
    sets: Mapping[str, PolicySet[R] | RefusedPolicySet],
    bindings: Mapping[str, PolicyBinding],
    subject: ServiceAccountRef,
) -> list[BindingResolution[R]]:
    """Every binding naming `subject`, in binding-name order."""
    return [
        resolve_binding(sets, bindings[name]) for name in sorted(bindings) if bindings[name].spec.subject == subject
    ]


def set_condition(
    binding: PolicyBinding, condition_type: str, *, ready: bool, reason: str, message: str, now: datetime
) -> Condition:
    """The condition to report, keeping the binding's transition time while its status holds."""
    status = ConditionStatus.TRUE if ready else ConditionStatus.FALSE
    previous = binding.condition(condition_type)
    return Condition(
        type=condition_type,
        status=status,
        reason=reason,
        message=message,
        observed_generation=binding.metadata.generation,
        last_transition_time=previous.last_transition_time
        if previous is not None and previous.status is status
        else now,
    )


class ConditionReporter:
    """Keeps one condition type on PolicyBindings equal to what its reader decided, applied
    server-side under the reader's own field manager so other readers' conditions stay."""

    def __init__(self, custom_objects: CustomObjectsClient, *, field_manager: str) -> None:
        self._custom_objects = custom_objects
        self._field_manager = field_manager
        # What this replica last applied, by binding UID, so a write is not repeated while its own
        # MODIFIED event is in flight and a recreated binding (same name, new UID) is judged afresh.
        self._written: dict[str, Condition] = {}

    async def report(self, decided: Iterable[tuple[PolicyBinding, Condition]]) -> None:
        held: set[str] = set()
        for binding, condition in decided:
            uid = binding.metadata.uid
            held.add(uid)
            if condition.says_same_as(binding.condition(condition.type)) or condition.says_same_as(
                self._written.get(uid)
            ):
                continue
            self._written[uid] = condition
            await self._apply(binding, condition)
        for uid in set(self._written) - held:
            del self._written[uid]

    async def _apply(self, binding: PolicyBinding, condition: Condition) -> None:
        metadata = binding.metadata
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "PolicyBinding",
            "metadata": {"name": metadata.name, "namespace": metadata.namespace},
            "status": {"conditions": [condition.model_dump(mode="json", by_alias=True, exclude_none=True)]},
        }
        try:
            await self._custom_objects.patch_namespaced_custom_object_status(
                GROUP,
                VERSION,
                metadata.namespace,
                POLICY_BINDINGS_PLURAL,
                metadata.name,
                body,
                _content_type=_APPLY_PATCH,
                field_manager=self._field_manager,
                force=True,
            )
        except k8s_client.ApiException as error:
            # The binding may be gone or edited meanwhile; the next event judges it again. Nothing
            # depends on the write, so an outage here is loud but not fatal.
            logger.warning("%s write for %s failed: %s %s", condition.type, metadata.name, error.status, error.reason)
            self._written.pop(metadata.uid, None)
