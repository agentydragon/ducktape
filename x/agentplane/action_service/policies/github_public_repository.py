"""`github_public_repository`: a listed GitHub MCP Action matches when the one repository it targets
is confirmed public by a live unauthenticated GitHub lookup. A lookup that fails or is unavailable
is not a match, so the request takes the human path; it is never a deny."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue

from github_policy.repository import evaluate_public_repository
from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policies.github_repository import from_repository_decision
from x.agentplane.action_service.policies.kind import Kind, Matched, NotMatched


class GitHubPublicRepository(Kind):
    type: Literal[PolicyKind.GITHUB_PUBLIC_REPOSITORY]


async def evaluate(
    policy: GitHubPublicRepository,
    action: ActionIdentity,
    arguments: Mapping[str, JsonValue],
    visibility: RepositoryVisibilityService,
) -> Matched | NotMatched:
    del policy
    return from_repository_decision(await evaluate_public_repository(action.name, arguments, visibility))
