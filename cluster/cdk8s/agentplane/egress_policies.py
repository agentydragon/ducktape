"""An egress policy: a `PolicySet` of `egress` rules."""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_policyset_crds.works.allegedly.agentplane import PolicySetSpecRules
from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.providers.agentplane import policy_set


def add_egress_policy(
    scope: Construct, id: str, *, metadata: ApiObjectMetadata, rules: Sequence[PolicySetSpecRules]
) -> None:
    policy_set.PolicySet(scope, id, metadata=metadata, rules=rules)
