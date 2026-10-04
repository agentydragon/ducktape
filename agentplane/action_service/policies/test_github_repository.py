"""`github_repository`: only calls targeting the configured pair match, and the match names it."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from pydantic import JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.models import MatchedRepository
from agentplane.action_service.policies.github_repository import GitHubRepository
from agentplane.action_service.policies.kind import Matched, NotMatched
from agentplane.action_service.policies.registry import evaluate

OWNER = "test-owner"
REPOSITORY = "test-repo"
POLICY = GitHubRepository.model_validate(
    {
        "type": "github_repository",
        "actions": {"github": ["get_file_contents", "search_pull_requests", "search_code"]},
        "owner": OWNER,
        "repository": REPOSITORY,
    }
)


def _github(name: str) -> ActionIdentity:
    return ActionIdentity(group="github", name=name)


@pytest.mark.parametrize(
    ("action", "arguments", "explanation"),
    [
        (
            _github("get_file_contents"),
            {"owner": "Test-Owner", "repo": "Test-Repo", "path": "README.md"},
            f"reviewed read targets repository {OWNER}/{REPOSITORY}",
        ),
        (
            _github("search_pull_requests"),
            {"owner": OWNER, "repo": REPOSITORY, "query": "is:open"},
            f"reviewed read targets repository {OWNER}/{REPOSITORY}",
        ),
        (
            _github("search_code"),
            {"query": f"repo:{OWNER}/{REPOSITORY} language:python"},
            f"reviewed code search targets repository {OWNER}/{REPOSITORY}",
        ),
    ],
)
async def test_configured_repository_matches_and_is_named(
    action: ActionIdentity,
    arguments: dict[str, JsonValue],
    explanation: str,
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    decision = await evaluate(POLICY, action, arguments, github_visibility())
    assert isinstance(decision, Matched)
    assert decision.explanation == explanation
    assert decision.repository is not None
    assert decision.repository.confirmed_public is False
    assert (decision.repository.owner.casefold(), decision.repository.repository.casefold()) == (OWNER, REPOSITORY)


async def test_match_records_the_repository_as_the_request_spelled_it(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    decision = await evaluate(
        POLICY,
        _github("get_file_contents"),
        {"owner": "Test-Owner", "repo": "Test-Repo", "path": "x"},
        github_visibility(),
    )
    assert isinstance(decision, Matched)
    assert decision.repository == MatchedRepository(owner="Test-Owner", repository="Test-Repo", confirmed_public=False)


async def test_a_smuggled_search_qualifier_does_not_match(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    """The rule layer's mismatch becomes `NotMatched`, judged by the Action's own tool name."""
    decision = await evaluate(
        POLICY,
        _github("search_pull_requests"),
        {"owner": OWNER, "repo": REPOSITORY, "query": "repo:x/y"},
        github_visibility(),
    )
    assert isinstance(decision, NotMatched)
    assert "qualifier" in decision.reason


if __name__ == "__main__":
    pytest_bazel.main()
