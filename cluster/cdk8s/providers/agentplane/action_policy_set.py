"""Ergonomic wrapper for Agentplane's own `ActionPolicySet` CRD
(agentplane/crds/manifests/crd-actionpolicysets.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind, and a named `@staticmethod` factory group for
`autoApproveIf`'s real variant shapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentplane_actionpolicyset_crds.works.allegedly.agentplane import (
    ActionPolicySet as _ActionPolicySet,
    ActionPolicySetSpec,
    ActionPolicySetSpecAutoApproveIf,
    ActionPolicySetSpecAutoApproveIfType,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class AutoApproveIf:
    """`ActionPolicySetSpecAutoApproveIf`'s real variant shapes this repo uses: `exact_actions`
    (an Action name allowlist), `github_repository` (a fixed owner/repository), and
    `github_public_repository` (a live, unauthenticated visibility check in place of a fixed
    owner/repository). The schema also defines `argument_schema` (also requires the arguments to
    satisfy a JSON Schema) and `home_assistant_entity_control` (a Home Assistant service call
    confined to configured entities and services) -- add a factory the day this repo builds one.

    """

    @staticmethod
    def exact_actions(*, actions: Mapping[str, Sequence[str]]) -> ActionPolicySetSpecAutoApproveIf:
        """Matches by Action name alone."""
        return ActionPolicySetSpecAutoApproveIf(
            type=ActionPolicySetSpecAutoApproveIfType.EXACT_UNDERSCORE_ACTIONS, actions=actions
        )

    @staticmethod
    def github_repository(
        *, owner: str, repository: str, actions: Mapping[str, Sequence[str]]
    ) -> ActionPolicySetSpecAutoApproveIf:
        """Requires a GitHub MCP call to target `owner`/`repository`."""
        return ActionPolicySetSpecAutoApproveIf(
            type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_REPOSITORY,
            owner=owner,
            repository=repository,
            actions=actions,
        )

    @staticmethod
    def github_public_repository(*, actions: Mapping[str, Sequence[str]]) -> ActionPolicySetSpecAutoApproveIf:
        """Requires a GitHub MCP call to target a repository a live, unauthenticated lookup
        confirms public."""
        return ActionPolicySetSpecAutoApproveIf(
            type=ActionPolicySetSpecAutoApproveIfType.GITHUB_UNDERSCORE_PUBLIC_UNDERSCORE_REPOSITORY, actions=actions
        )


class ActionPolicySet(_ActionPolicySet):
    """Agentplane's `ActionPolicySet`: a reusable, subject-free set of Action auto-approval
    policies. Build `auto_approve_if` entries with `AutoApproveIf`'s factories.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        auto_approve_if: Sequence[ActionPolicySetSpecAutoApproveIf] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ActionPolicySetSpec(auto_approve_if=list(auto_approve_if) if auto_approve_if is not None else None),
        )
