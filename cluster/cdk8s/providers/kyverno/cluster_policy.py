"""Typed helpers for common Kyverno ClusterPolicy rule shapes.

Fields the CRD schema itself leaves untyped (`x-kubernetes-preserve-unknown-fields`) --
`preconditions`, `validate.deny.conditions`, `mutate.patchStrategicMerge`,
`generate.data` -- have no factory here; a caller passes them as plain dicts straight
into the generated struct, per cluster/skills/cdk8s_builders/SKILL.md's escape-hatch
guidance.
"""

from __future__ import annotations

from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicySpecRulesMatch,
    ClusterPolicySpecRulesMatchAny,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesValidate,
    ClusterPolicySpecRulesValidateDeny,
)


def match_resources(resources: ClusterPolicySpecRulesMatchAnyResources) -> ClusterPolicySpecRulesMatch:
    """A `match` selecting by resource kind/operation/namespace/selector alone, with no
    subject/role restriction -- the common case among this repo's rules. Kyverno's
    `match.any`/`match.all` entries also combine `resources` with `subjects`/`roles`/
    `clusterRoles`, and `all` ANDs several entries instead of ORing them; build
    `ClusterPolicySpecRulesMatch` directly for either of those.
    """
    return ClusterPolicySpecRulesMatch(any=[ClusterPolicySpecRulesMatchAny(resources=resources)])


class Validate:
    """`ClusterPolicySpecRulesValidate`'s real variant shape this repo uses: `deny`
    (a JMESPath condition block, denying unconditionally when omitted). The schema also
    defines `cel`/`pattern`/`anyPattern`/`podSecurity`/`manifests`/`assert` -- add a
    factory for one the day this repo builds it.
    """

    @staticmethod
    def deny(*, message: str, conditions: dict[str, object] | None = None) -> ClusterPolicySpecRulesValidate:
        """Denies the request when `conditions` evaluates true, or unconditionally when
        omitted. `conditions` is `deny.conditions`, which the CRD schema leaves untyped
        (`x-kubernetes-preserve-unknown-fields`) -- pass it as a raw dict."""
        return ClusterPolicySpecRulesValidate(
            message=message, deny=ClusterPolicySpecRulesValidateDeny(conditions=conditions)
        )
