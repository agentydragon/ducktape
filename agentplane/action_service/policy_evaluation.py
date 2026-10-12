"""A caller's bindings as they stand at admission, and the provider that decides from them.

The kinds themselves live in `policies/`. Evaluation happens once, at admission, against the
objects the informer holds then; a later edit, expiry or deletion changes the next Action's
Decision, not this one's, so the Decision records what it saw.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.models import (
    BindingEvidence,
    MatchedPolicy,
    NamespacedName,
    PolicyEvidence,
    PolicySetEvidence,
    PolicySource,
    ProviderOutcome,
    ProviderVerdict,
)
from agentplane.action_service.policies.kind import Matched, NotMatched
from agentplane.action_service.policies.registry import Policy, evaluate, lists
from agentplane.action_service.policies.resources import ActionPolicySet, InvalidResource
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.providers import BindingKind, DecisionContext, ResolvedBinding, ResolvedPolicySet
from agentplane.policy_sets import PolicyBinding, PolicySet, subject_bindings
from agentplane.subjects import ServiceAccountRef

PROVIDER_NAME = "action_policy_set"
AUTO_APPROVE_REASON = "policy_set_auto_approve"
NO_MATCH_REASON = "no_auto_approve_match"
# ProviderOutcome bounds the explanation; object names alone can approach it.
_DESCRIPTION_LIMIT = 500


def resolve_bindings(index: PolicyIndex, caller: ServiceAccountRef, now: datetime) -> tuple[ResolvedBinding, ...]:
    """The caller's bindings, each with the valid sets it names that exist; a set it names that is
    missing or invalid contributes nothing. Nothing before sync.

    First its unexpired, valid `ActionPolicyBinding`s in key order, then its `PolicyBinding`s in name
    order, which the Policy Controller deletes at expiry. A grant's caller is matched by the
    ServiceAccount behind it; the operator asks about that ServiceAccount directly, without a grant."""
    if not index.synced:
        return ()
    resolved: list[ResolvedBinding] = []
    for key in sorted(index.bindings):
        binding = index.bindings[key]
        if isinstance(binding, InvalidResource):
            continue
        spec = binding.spec
        if spec.expires_at is not None and spec.expires_at <= now:
            continue
        if spec.subject != caller:
            continue
        resolved.append(
            ResolvedBinding(
                kind=BindingKind.ACTION_POLICY_BINDING,
                namespace=binding.metadata.namespace,
                name=binding.metadata.name,
                resource_version=binding.metadata.resource_version,
                expires_at=spec.expires_at,
                policy_sets=tuple(
                    _action_policy_set(policy_set)
                    for name in spec.policy_sets
                    if isinstance(
                        policy_set := index.policy_sets.get(NamespacedName(binding.metadata.namespace, name)),
                        ActionPolicySet,
                    )
                ),
            )
        )
    resolved.extend(
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
    return tuple(resolved)


def _action_policy_set(policy_set: ActionPolicySet) -> ResolvedPolicySet:
    return ResolvedPolicySet(
        namespace=policy_set.metadata.namespace,
        name=policy_set.metadata.name,
        generation=policy_set.metadata.generation,
        source=PolicySource.AUTO_APPROVE_IF,
        policies=tuple(enumerate(policy_set.spec.auto_approve_if)),
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


def auto_approvable(bindings: Sequence[ResolvedBinding]) -> frozenset[ActionIdentity]:
    """Every Action some auto-approve policy of these bindings names, whatever its arguments."""
    return frozenset(
        ActionIdentity(group=group, name=name)
        for resolved in bindings
        for policy_set in resolved.policy_sets
        for _, policy in policy_set.policies
        for group, names in policy.actions.items()
        for name in names
    )


def _evidence(context: DecisionContext, matched: MatchedPolicy) -> PolicyEvidence:
    sets = {
        (policy_set.source, policy_set.namespace, policy_set.name): policy_set
        for resolved in context.bindings
        for policy_set in resolved.policy_sets
    }
    return PolicyEvidence(
        bindings=[
            BindingEvidence(namespace=binding.namespace, name=binding.name, resource_version=binding.resource_version)
            for binding in context.bindings
        ],
        policy_sets=[
            PolicySetEvidence(namespace=policy_set.namespace, name=policy_set.name, generation=policy_set.generation)
            for policy_set in sets.values()
        ],
        matched=matched,
    )


class PolicySetDecisionProvider:
    """Allows a request the caller's bindings auto-approve; anything else is no opinion.

    Auto-approval is the only form an Action policy takes, an `ActionPolicySet`'s `autoApproveIf` or a
    `PolicySet`'s `action_auto_approve` rules; there is no deny form, so what does not match waits for
    a human.
    """

    name = PROVIDER_NAME

    def __init__(self, *, visibility: RepositoryVisibilityService) -> None:
        self._visibility = visibility

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        # Why each policy naming this Action did not match. No Decision records a no-opinion, so
        # this reaches only a direct caller refused because nothing decided.
        declined: list[str] = []
        for resolved in context.bindings:
            for policy_set in resolved.policy_sets:
                for index, policy in policy_set.policies:
                    match await evaluate(policy, context.action, context.arguments, self._visibility):
                        case Matched(explanation=explanation, repository=repository):
                            return ProviderOutcome(
                                verdict=ProviderVerdict.ALLOW,
                                reason_code=AUTO_APPROVE_REASON,
                                reason_description=(
                                    f"binding {resolved.namespace}/{resolved.name} set {policy_set.name} "
                                    f"{policy_set.source}[{index}] {policy.type}: {explanation}"
                                )[:_DESCRIPTION_LIMIT],
                                evidence=_evidence(
                                    context,
                                    MatchedPolicy(
                                        namespace=policy_set.namespace,
                                        policy_set=policy_set.name,
                                        source=policy_set.source,
                                        index=index,
                                        type=policy.type,
                                        repository=repository,
                                    ),
                                ),
                            )
                        case NotMatched(reason=reason):
                            if lists(policy, context.action):
                                declined.append(
                                    f"set {policy_set.name} {policy_set.source}[{index}] {policy.type}: {reason}"
                                )
        action = context.action
        return ProviderOutcome(
            verdict=ProviderVerdict.NO_OPINION,
            reason_code=NO_MATCH_REASON,
            reason_description=(
                "; ".join(declined)
                if declined
                else f"no auto-approve policy bound to the caller lists {action.group}/{action.name}"
            )[:_DESCRIPTION_LIMIT],
        )
