"""`github_repository`: a listed GitHub MCP Action matches when the one repository it targets is the
configured `owner`/`repository`. The target and its search-qualifier boundaries are the shared
`github_policy.repository` rules, the same the Haku console applies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, JsonValue

from github_policy.repository import RepositoryMatch, RepositoryMismatch, evaluate_fixed_repository
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import MatchedRepository, PolicyKind
from x.agentplane.action_service.policies.kind import Kind, Matched, NotMatched


class GitHubRepository(Kind):
    type: Literal[PolicyKind.GITHUB_REPOSITORY]
    owner: str = Field(min_length=1)
    repository: str = Field(min_length=1)


def from_repository_decision(decision: RepositoryMatch | RepositoryMismatch) -> Matched | NotMatched:
    match decision:
        case RepositoryMatch(target=target, confirmed_public=confirmed_public, explanation=explanation):
            return Matched(
                explanation,
                repository=MatchedRepository(
                    owner=target.owner, repository=target.repository, confirmed_public=confirmed_public
                ),
            )
        case RepositoryMismatch(reason=reason):
            return NotMatched(reason)


def evaluate(
    policy: GitHubRepository, action: ActionIdentity, arguments: Mapping[str, JsonValue]
) -> Matched | NotMatched:
    return from_repository_decision(evaluate_fixed_repository(action.name, arguments, policy.owner, policy.repository))
