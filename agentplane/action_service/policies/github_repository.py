"""The `github_repository` policy model, scoped to one configured owner and repository."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind


class GitHubRepository(Kind):
    type: Literal[PolicyKind.GITHUB_REPOSITORY]
    owner: str = Field(min_length=1)
    repository: str = Field(min_length=1)
