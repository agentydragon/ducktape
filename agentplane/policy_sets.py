"""`PolicySet` and `PolicyBinding` as their Python readers see them (agentplane/crds/manifests/).

Each reader parses only the rule types it enforces, resolves a subject's bindings to sets, and keeps
a status condition of its own on every binding. Expiry is not a reader's concern: the Policy
Controller deletes a binding once its `expiresAt` passes, so a binding that exists grants.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from kubernetes_asyncio import client as k8s_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
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


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True, alias_generator=to_camel)


class ObjectMeta(_Wire):
    name: str
    namespace: str
    uid: str
    generation: int


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


class _RawSpec(_Wire):
    rules: list[dict[str, Any]]


class _RawPolicySet(_Wire):
    metadata: ObjectMeta
    spec: _RawSpec


def parse_policy_set[R](raw: object, *, types: Set[str], parse_rule: Callable[[dict[str, Any]], R]) -> PolicySet[R]:
    """Keeps the rules whose `type` is in `types`, each parsed by `parse_rule`; the rest belong to
    another reader."""
    policy_set = _RawPolicySet.model_validate(raw)
    return PolicySet(
        name=policy_set.metadata.name,
        rules=tuple(
            NumberedRule(number=number, rule=parse_rule(rule))
            for number, rule in enumerate(policy_set.spec.rules)
            if rule["type"] in types
        ),
    )


@dataclass(frozen=True)
class BindingResolution[R]:
    """A binding and the sets it names, as they stand: a missing set takes nothing from the others."""

    binding: PolicyBinding
    sets: tuple[PolicySet[R], ...]
    missing: tuple[str, ...]


def resolve_binding[R](sets: Mapping[str, PolicySet[R]], binding: PolicyBinding) -> BindingResolution[R]:
    names = binding.spec.policy_sets
    return BindingResolution(
        binding=binding,
        sets=tuple(sets[name] for name in names if name in sets),
        missing=tuple(name for name in names if name not in sets),
    )


def subject_bindings[R](
    sets: Mapping[str, PolicySet[R]], bindings: Mapping[str, PolicyBinding], subject: ServiceAccountRef
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
