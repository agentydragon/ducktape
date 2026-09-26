"""Ergonomic wrapper for Kyverno's `ClusterPolicy`, following cdk8s-plus's own
construction pattern: a class named after the kind, and named `@classmethod` factories
grouping a spec fragment's real variant shapes under one type.

Fields the CRD schema itself leaves untyped (`x-kubernetes-preserve-unknown-fields`) --
`preconditions`, `validate.deny.conditions`, `mutate.patchStrategicMerge`,
`generate.data` -- have no factory here; a caller passes them as plain dicts straight
into the generated struct, per cluster/skills/cdk8s_builders/SKILL.md's escape-hatch
guidance.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicy as _ClusterPolicy,
    ClusterPolicySpec,
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesMatch,
    ClusterPolicySpecRulesMatchAny,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesValidate,
    ClusterPolicySpecRulesValidateCel,
    ClusterPolicySpecRulesValidateCelExpressions,
    ClusterPolicySpecRulesValidateDeny,
    ClusterPolicySpecValidationFailureAction,
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
    """`ClusterPolicySpecRulesValidate`'s real variant shapes this repo uses: `deny`
    (a JMESPath condition block, denying unconditionally when omitted) and `cel` (CEL
    expressions). The schema also defines `pattern`/`anyPattern`/`podSecurity`/
    `manifests`/`assert` -- add a factory for one the day this repo builds it.
    """

    def __init__(self, spec: ClusterPolicySpecRulesValidate) -> None:
        self._spec = spec

    def to_spec(self) -> ClusterPolicySpecRulesValidate:
        return self._spec

    @classmethod
    def deny(cls, *, message: str, conditions: dict[str, object] | None = None) -> Validate:
        """Denies the request when `conditions` evaluates true, or unconditionally when
        omitted. `conditions` is `deny.conditions`, which the CRD schema leaves untyped
        (`x-kubernetes-preserve-unknown-fields`) -- pass it as a raw dict."""
        return cls(
            ClusterPolicySpecRulesValidate(
                message=message, deny=ClusterPolicySpecRulesValidateDeny(conditions=conditions)
            )
        )

    @classmethod
    def cel(cls, *, message: str, expressions: Sequence[ClusterPolicySpecRulesValidateCelExpressions]) -> Validate:
        """Denies the request unless every CEL `expressions` entry evaluates true."""
        return cls(
            ClusterPolicySpecRulesValidate(
                message=message, cel=ClusterPolicySpecRulesValidateCel(expressions=list(expressions))
            )
        )


class ClusterPolicy(_ClusterPolicy):
    """Kyverno's `ClusterPolicy`. Keywords are `ClusterPolicySpec` fields under their
    own names; `None` leaves a field unset, so Kyverno's own default applies.
    `validation_failure_action` sets the deprecated but still-served
    `spec.validationFailureAction`; Kyverno's newer, per-rule `validate.failureAction`
    supersedes it but is unused by this repo today.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        rules: Sequence[ClusterPolicySpecRules],
        background: bool | None = None,
        admission: bool | None = None,
        mutate_existing_on_policy_update: bool | None = None,
        validation_failure_action: ClusterPolicySpecValidationFailureAction | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ClusterPolicySpec(
                rules=list(rules),
                background=background,
                admission=admission,
                mutate_existing_on_policy_update=mutate_existing_on_policy_update,
                validation_failure_action=validation_failure_action,
            ),
        )
