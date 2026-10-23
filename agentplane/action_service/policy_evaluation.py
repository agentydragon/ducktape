"""Resolve a caller's current policy bindings for the app's policy views."""

from __future__ import annotations

from agentplane.action_service.models import PolicySource
from agentplane.action_service.policies.registry import Policy
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.providers import BindingKind, ResolvedBinding, ResolvedPolicySet
from agentplane.policy_sets import PolicyBinding, PolicySet, subject_bindings
from agentplane.subjects import ServiceAccountRef


def resolve_bindings(index: PolicyIndex, caller: ServiceAccountRef) -> tuple[ResolvedBinding, ...]:
    """The caller's `PolicyBinding`s in name order (the Policy Controller deletes one at expiry), each
    with the valid sets it names that exist; a set it names that is missing or refused contributes
    nothing. Nothing before sync. A grant's caller is matched by the ServiceAccount behind it; the
    operator asks about that ServiceAccount directly, without a grant."""
    if not index.synced:
        return ()
    return tuple(
        ResolvedBinding(
            kind=BindingKind.POLICY_BINDING,
            namespace=resolution.binding.metadata.namespace,
            name=resolution.binding.metadata.name,
            resource_version=resolution.binding.metadata.resource_version,
            expires_at=resolution.binding.spec.expires_at,
            policy_sets=tuple(_rule_set(resolution.binding, policy_set) for policy_set in resolution.sets),
        )
        for resolution in subject_bindings(index.rule_sets, index.policy_bindings, caller)
    )


def _rule_set(binding: PolicyBinding, policy_set: PolicySet[Policy]) -> ResolvedPolicySet:
    assert policy_set.generation is not None, "the informer's sets are read off the API server"
    return ResolvedPolicySet(
        namespace=binding.metadata.namespace,
        name=policy_set.name,
        generation=policy_set.generation,
        source=PolicySource.RULES,
        policies=tuple((numbered.number, numbered.rule) for numbered in policy_set.rules),
    )
