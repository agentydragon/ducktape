"""The list-and-watch every reader of `PolicySet`s and `PolicyBinding`s shares.

A reader supplies the rule types it parses, the condition it keeps on each binding and whatever
else it watches; this keeps the two kinds in its stores equal to the API server's, stamps the
freshness of every kind it watches, and reports the condition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_watch import Freshness
from agentplane.policy_sets import (
    POLICY_BINDINGS_PLURAL,
    POLICY_SET_UNAVAILABLE,
    POLICY_SETS_PLURAL,
    Condition,
    ConditionReporter,
    PolicyBinding,
    PolicySet,
    RefusedPolicySet,
    parse_policy_set,
    resolve_binding,
    set_condition,
    unavailable_message,
)
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to


@dataclass(frozen=True)
class ReadyCondition:
    """The condition one reader keeps on every `PolicyBinding`: `True` while every set the binding
    names exists and parses for that reader."""

    type: str
    field_manager: str
    applied_reason: str
    # `True` reads `<rules_label>: N` for the N rules the binding's sets grant this reader.
    rules_label: str


class PolicyWatch[R]:
    def __init__(
        self,
        *,
        custom_objects: CustomObjectsClient,
        namespace: str,
        parse_rule: Callable[[dict[str, Any]], R | None],
        policy_sets: dict[str, PolicySet[R] | RefusedPolicySet],
        policy_bindings: dict[str, PolicyBinding],
        condition: ReadyCondition,
        freshness: Freshness,
        other_kinds: Sequence[WatchedKind],
        resync_seconds: int,
        clock: Callable[[], datetime],
        mark_synced: Callable[[bool], None],
        notify: Callable[[], Awaitable[None]],
    ) -> None:
        """Keeps `policy_sets` and `policy_bindings` equal to `namespace`'s, alongside `other_kinds`.

        `mark_synced` hears whether every kind has listed before each report, and `notify` fires
        after every change and every completed cycle.
        """
        self._policy_sets = policy_sets
        self._policy_bindings = policy_bindings
        self._condition = condition
        self._freshness = freshness
        self._clock = clock
        self._mark_synced = mark_synced
        self._notify = notify
        self._reporter = ConditionReporter(custom_objects, field_manager=condition.field_manager)
        self._watch = ListWatch(
            kinds=(
                WatchedKind(
                    name=POLICY_SETS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICY_SETS_PLURAL),
                    parse=partial(parse_policy_set, parse_rule=parse_rule),
                    key=lambda policy_set: policy_set.name,
                    names=lambda: set(policy_sets),
                    apply=lambda name, obj: apply_to(policy_sets, name, obj),
                ),
                WatchedKind(
                    name=POLICY_BINDINGS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICY_BINDINGS_PLURAL),
                    parse=PolicyBinding.model_validate,
                    key=lambda binding: binding.metadata.name,
                    names=lambda: set(policy_bindings),
                    apply=lambda name, obj: apply_to(policy_bindings, name, obj),
                ),
                *other_kinds,
            ),
            resync_seconds=resync_seconds,
            on_change=self._changed,
            on_cycle=self._completed,
            clock=clock,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    async def _changed(self, kind: WatchedKind) -> None:
        synced = self._watch.synced
        self._mark_synced(synced)
        # Until every kind has listed, a set a binding names may merely not have been seen.
        if synced:
            now = self._clock()
            await self._reporter.report(
                (binding, self._ready(binding, now)) for binding in list(self._policy_bindings.values())
            )
        await self._notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        self._freshness.record(kind.name, at)
        await self._notify()

    def _ready(self, binding: PolicyBinding, now: datetime) -> Condition:
        """Whether every set the binding names exists and parses; the sets that do grant either way."""
        resolution = resolve_binding(self._policy_sets, binding)
        if resolution.missing or resolution.refused:
            return set_condition(
                binding,
                self._condition.type,
                ready=False,
                reason=POLICY_SET_UNAVAILABLE,
                message=unavailable_message(resolution),
                now=now,
            )
        rules = sum(len(policy_set.rules) for policy_set in resolution.sets)
        return set_condition(
            binding,
            self._condition.type,
            ready=True,
            reason=self._condition.applied_reason,
            message=f"{self._condition.rules_label}: {rules}",
            now=now,
        )
