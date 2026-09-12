"""A caller's bindings as they stand at admission, and the provider that decides from them.

The kinds themselves live in `policies/`. Evaluation happens once, at admission, against the
objects the informer holds then; a later edit, expiry or deletion changes the next Action's
Decision, not this one's, so the Decision records what it saw.
"""

from __future__ import annotations

from datetime import datetime

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.models import (
    BindingEvidence,
    MatchedPolicy,
    NamespacedName,
    PolicyEvidence,
    PolicySetEvidence,
    ProviderOutcome,
    ProviderVerdict,
    SandboxCaller,
    ServiceAccountCaller,
    ServiceAccountRef,
)
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import evaluate
from x.agentplane.action_service.policies.resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    SandboxSubject,
    ServiceAccountSubject,
)
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.action_service.providers import DecisionContext, ResolvedBinding

PROVIDER_NAME = "action_policy_set"
AUTO_APPROVE_REASON = "policy_set_auto_approve"
NO_MATCH_REASON = "no_auto_approve_match"
# ProviderOutcome bounds the explanation; object names alone can approach it.
_DESCRIPTION_LIMIT = 500


def _names(
    subject: SandboxSubject | ServiceAccountSubject, namespace: str, named: SandboxCaller | ServiceAccountRef
) -> bool:
    """Whether a binding in `namespace` with this subject names the caller. A Sandbox is matched by
    namespace and UID; its name is for humans."""
    match subject:
        case SandboxSubject(sandbox=sandbox):
            return (
                isinstance(named, SandboxCaller) and named.namespace == namespace and named.sandbox_uid == sandbox.uid
            )
        case ServiceAccountSubject(service_account=account):
            return isinstance(named, ServiceAccountRef) and named == account


def resolve_bindings(
    index: PolicyIndex, caller: SandboxCaller | ServiceAccountCaller | ServiceAccountRef, now: datetime
) -> tuple[ResolvedBinding, ...]:
    """The caller's unexpired, valid bindings in key order, each with the valid sets it names that
    exist; a set it names that is missing or invalid contributes nothing. Nothing before sync.

    A grant's caller is matched by the ServiceAccount behind it; the operator asks about that
    ServiceAccount directly, without a grant."""
    if not index.synced:
        return ()
    named = caller.service_account if isinstance(caller, ServiceAccountCaller) else caller
    resolved: list[ResolvedBinding] = []
    for key in sorted(index.bindings):
        binding = index.bindings[key]
        if isinstance(binding, InvalidResource):
            continue
        spec = binding.spec
        if spec.expires_at is not None and spec.expires_at <= now:
            continue
        if not _names(spec.subject, binding.metadata.namespace, named):
            continue
        sets = tuple(
            policy_set
            for name in spec.policy_sets
            if isinstance(
                policy_set := index.policy_sets.get(NamespacedName(binding.metadata.namespace, name)), ActionPolicySet
            )
        )
        resolved.append(ResolvedBinding(binding=binding, policy_sets=sets))
    return tuple(resolved)


def _evidence(context: DecisionContext, matched: MatchedPolicy) -> PolicyEvidence:
    bindings: list[ActionPolicyBinding] = [resolved.binding for resolved in context.bindings]
    sets = {
        policy_set.namespaced_name: policy_set for resolved in context.bindings for policy_set in resolved.policy_sets
    }
    return PolicyEvidence(
        bindings=[
            BindingEvidence(
                namespace=binding.metadata.namespace,
                name=binding.metadata.name,
                resource_version=binding.metadata.resource_version,
            )
            for binding in bindings
        ],
        policy_sets=[
            PolicySetEvidence(
                namespace=policy_set.metadata.namespace,
                name=policy_set.metadata.name,
                generation=policy_set.metadata.generation,
            )
            for policy_set in sets.values()
        ],
        matched=matched,
    )


class PolicySetDecisionProvider:
    """Allows a request the caller's bindings auto-approve; anything else is no opinion.

    Only `autoApproveIf` decides here. `autoDenyIf` and `autoDenyUnless` are parsed, validated and
    reported Ready like the rest of a set, and produce no vote in this version.
    """

    name = PROVIDER_NAME

    def __init__(self, *, visibility: RepositoryVisibilityService) -> None:
        self._visibility = visibility

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        for resolved in context.bindings:
            for policy_set in resolved.policy_sets:
                for index, policy in enumerate(policy_set.spec.auto_approve_if):
                    match await evaluate(policy, context.action, context.arguments, self._visibility):
                        case Matched(explanation=explanation, repository=repository):
                            binding, metadata = resolved.binding.metadata, policy_set.metadata
                            return ProviderOutcome(
                                verdict=ProviderVerdict.ALLOW,
                                reason_code=AUTO_APPROVE_REASON,
                                reason_description=(
                                    f"binding {binding.namespace}/{binding.name} set {metadata.name} "
                                    f"autoApproveIf[{index}] {policy.type}: {explanation}"
                                )[:_DESCRIPTION_LIMIT],
                                evidence=_evidence(
                                    context,
                                    MatchedPolicy(
                                        namespace=metadata.namespace,
                                        policy_set=metadata.name,
                                        source="autoApproveIf",
                                        index=index,
                                        type=policy.type,
                                        repository=repository,
                                    ),
                                ),
                            )
                        case NotMatched():
                            continue
        return ProviderOutcome(verdict=ProviderVerdict.NO_OPINION, reason_code=NO_MATCH_REASON)
