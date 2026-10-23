"""The `github_public_repository` policy model; the Go runtime checks target visibility."""

from __future__ import annotations

from typing import Literal

from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind


class GitHubPublicRepository(Kind):
    type: Literal[PolicyKind.GITHUB_PUBLIC_REPOSITORY]
