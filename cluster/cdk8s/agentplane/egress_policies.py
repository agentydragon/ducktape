"""An egress policy, rendered for both kinds of binding the egress proxy reads: an `EgressPolicy` for
the `EgressBinding`s the Sandbox Service writes, and a `PolicySet` of the same name and `egress`
rules for `PolicyBinding`s.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_egresspolicy_crds.works.allegedly.agentplane import EgressPolicySpecRules
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRules,
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.providers.agentplane import policy_set
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy


def _egress_rule(rule: EgressPolicySpecRules) -> PolicySetSpecRules:
    return policy_set.Rule.egress(
        hosts=rule.hosts,
        methods=None if rule.methods is None else [PolicySetSpecRulesMethods[method.name] for method in rule.methods],
        paths=rule.paths,
        cluster_internal=rule.cluster_internal,
        credential_ref=None
        if rule.credential_ref is None
        else PolicySetSpecRulesCredentialRef(name=rule.credential_ref.name),
    )


def add_egress_policy(
    scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[EgressPolicySpecRules]
) -> None:
    # CLEANUP(added 2026-10-12): Drop the EgressPolicy, and build `egress` rules directly, once
    #   the Sandbox Service binds Sandboxes through PolicyBindings and no EgressBinding exists.
    EgressPolicy(scope, id, metadata=metadata, rules=rules)
    policy_set.PolicySet(scope, f"{id}-set", metadata=metadata, rules=[_egress_rule(rule) for rule in rules])
