"""Ergonomic wrapper for Agentplane's own `ActionPolicySet` CRD
(cluster/k8s/agentplane-crds/crd-actionpolicysets.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind, and a named `@classmethod` factory group for
`autoApproveIf`'s real variant shapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentplane_actionpolicyset_crds.works.allegedly.agentplane import (
    ActionPolicySet as _ActionPolicySet,
    ActionPolicySetSpec,
    ActionPolicySetSpecAutoApproveIf,
    ActionPolicySetSpecAutoApproveIfType,
    ActionPolicySetSpecAutoDenyIf,
    ActionPolicySetSpecAutoDenyUnless,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class AutoApproveIf:
    """`ActionPolicySetSpecAutoApproveIf`'s real variant shapes this repo uses: `exact_actions`
    (an Action name allowlist), `github_repository` (a fixed owner/repository), and
    `github_public_repository` (a live, unauthenticated visibility check in place of a fixed
    owner/repository). The schema also defines `argument_schema` (also requires the arguments to
    satisfy a JSON Schema) -- add a factory the day this repo builds it.

    `autoDenyIf`/`autoDenyUnless` are the same schema fragment (the `&policy` YAML anchor),
    repeated for two other `ActionPolicySetSpec` fields neither this class nor any construction
    site covers today; `cdk8s_import` generates each occurrence as its own Python type
    (`ActionPolicySetSpecAutoDenyIf`/`Unless`, each with its own `Type` enum) rather than sharing
    this one, so an analogous factory class is what a caller of one of those fields would add.
    """

    def __init__(self, spec: ActionPolicySetSpecAutoApproveIf) -> None:
        self._spec = spec

    def to_spec(self) -> ActionPolicySetSpecAutoApproveIf:
        return self._spec

    @classmethod
    def exact_actions(cls, *, actions: Mapping[str, Sequence[str]]) -> AutoApproveIf:
        """Matches by Action name alone."""
        return cls(
            ActionPolicySetSpecAutoApproveIf(
                type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS, actions=actions
            )
        )

    @classmethod
    def github_repository(cls, *, owner: str, repository: str, actions: Mapping[str, Sequence[str]]) -> AutoApproveIf:
        """Requires a GitHub MCP call to target `owner`/`repository`."""
        return cls(
            ActionPolicySetSpecAutoApproveIf(
                type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_REPOSITORY,
                owner=owner,
                repository=repository,
                actions=actions,
            )
        )

    @classmethod
    def github_public_repository(cls, *, actions: Mapping[str, Sequence[str]]) -> AutoApproveIf:
        """Requires a GitHub MCP call to target a repository a live, unauthenticated lookup
        confirms public."""
        return cls(
            ActionPolicySetSpecAutoApproveIf(
                type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_PUBLIC_UNDERSCORE_REPOSITORY,
                actions=actions,
            )
        )


class ActionPolicySet(_ActionPolicySet):
    """Agentplane's `ActionPolicySet`: a reusable, subject-free set of Action auto-decision
    policies. Build `auto_approve_if` entries with `AutoApproveIf`'s factories; `auto_deny_if`/
    `auto_deny_unless` take their own CRD-generated structs raw (see `AutoApproveIf`'s docstring)
    since no factory covers them yet.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        auto_approve_if: Sequence[ActionPolicySetSpecAutoApproveIf] | None = None,
        auto_deny_if: Sequence[ActionPolicySetSpecAutoDenyIf] | None = None,
        auto_deny_unless: Sequence[ActionPolicySetSpecAutoDenyUnless] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ActionPolicySetSpec(
                auto_approve_if=list(auto_approve_if) if auto_approve_if is not None else None,
                auto_deny_if=list(auto_deny_if) if auto_deny_if is not None else None,
                auto_deny_unless=list(auto_deny_unless) if auto_deny_unless is not None else None,
            ),
        )
