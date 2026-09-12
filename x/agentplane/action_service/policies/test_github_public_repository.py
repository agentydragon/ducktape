"""`github_public_repository`: only a target the live lookup confirms public matches; a private,
unknown, or unreachable one does not, and never denies."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from pydantic import JsonValue, ValidationError

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import MatchedRepository
from x.agentplane.action_service.policies.github_public_repository import GitHubPublicRepository
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import evaluate

PUBLIC = ("someone", "public-thing")
POLICY = GitHubPublicRepository.model_validate(
    {"type": "github_public_repository", "actions": {"github": ["get_file_contents", "search_code"]}}
)
GET_FILE = ActionIdentity(group="github", name="get_file_contents")


async def test_confirmed_public_target_matches_and_records_the_confirmation(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    decision = await evaluate(
        POLICY, GET_FILE, {"owner": "someone", "repo": "public-thing", "path": "README.md"}, github_visibility(PUBLIC)
    )
    assert decision == Matched(
        "reviewed read targets confirmed-public repository someone/public-thing",
        repository=MatchedRepository(owner="someone", repository="public-thing", confirmed_public=True),
    )


async def test_code_search_confirms_the_qualifier_repository(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    decision = await evaluate(
        POLICY,
        ActionIdentity(group="github", name="search_code"),
        {"query": "repo:someone/public-thing language:python"},
        github_visibility(PUBLIC),
    )
    assert isinstance(decision, Matched)
    assert decision.repository == MatchedRepository(owner="someone", repository="public-thing", confirmed_public=True)


@pytest.mark.parametrize(
    ("arguments", "public", "unavailable", "reason"),
    [
        ({"owner": "someone", "repo": "private-thing", "path": "x"}, (PUBLIC,), False, "not confirmed public"),
        ({"owner": "someone", "repo": "public-thing", "path": "x"}, (PUBLIC,), True, "could not confirm"),
        ({"owner": "someone", "path": "x"}, (PUBLIC,), False, "string owner/repo"),
    ],
    ids=["private", "lookup-unavailable", "no-target"],
)
async def test_unconfirmed_targets_do_not_match(
    arguments: dict[str, JsonValue],
    public: tuple[tuple[str, str], ...],
    unavailable: bool,
    reason: str,
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    decision = await evaluate(POLICY, GET_FILE, arguments, github_visibility(*public, unavailable=unavailable))
    assert isinstance(decision, NotMatched)
    assert reason in decision.reason


def test_names_no_repository_of_its_own() -> None:
    with pytest.raises(ValidationError, match="owner"):
        GitHubPublicRepository.model_validate(
            {"type": "github_public_repository", "actions": {"github": ["get_file_contents"]}, "owner": "someone"}
        )


if __name__ == "__main__":
    pytest_bazel.main()
