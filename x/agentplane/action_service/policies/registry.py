"""Every policy kind, as the `type`-discriminated union a set's lists hold and the dispatch that
evaluates one policy against one request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import Field, JsonValue

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.policies import (
    argument_schema,
    exact_actions,
    github_public_repository,
    github_repository,
)
from x.agentplane.action_service.policies.kind import Matched, NotMatched

Policy = Annotated[
    exact_actions.ExactActions
    | argument_schema.ArgumentSchema
    | github_repository.GitHubRepository
    | github_public_repository.GitHubPublicRepository,
    Field(discriminator="type"),
]


async def evaluate(
    policy: Policy, action: ActionIdentity, arguments: Mapping[str, JsonValue], visibility: RepositoryVisibilityService
) -> Matched | NotMatched:
    """The Action must be listed, and the kind's own test must pass."""
    if action.name not in policy.actions.get(action.group, frozenset()):
        return NotMatched(f"{action.group}/{action.name} is not listed")
    match policy:
        case exact_actions.ExactActions():
            return exact_actions.evaluate(policy, action)
        case argument_schema.ArgumentSchema():
            return argument_schema.evaluate(policy, action, arguments)
        case github_repository.GitHubRepository():
            return github_repository.evaluate(policy, action, arguments)
        case github_public_repository.GitHubPublicRepository():
            return await github_public_repository.evaluate(policy, action, arguments, visibility)
