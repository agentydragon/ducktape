"""Target derivation and the two repository policies: the smuggling boundaries each search tool has."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from github_policy.repository import (
    RepositoryMatch,
    RepositoryMismatch,
    TargetRepository,
    evaluate_fixed_repository,
    evaluate_public_repository,
)
from github_policy.visibility import RepositoryVisibilityService

OWNER = "test-owner"
REPOSITORY = "test-repo"


def _fixed(tool_name: str, arguments: dict[str, object]) -> RepositoryMatch | RepositoryMismatch:
    return evaluate_fixed_repository(tool_name, arguments, OWNER, REPOSITORY)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_file_contents", {"owner": OWNER, "repo": REPOSITORY, "path": "README.md"}),
        ("issue_read", {"owner": "Test-Owner", "repo": "Test-Repo", "issue_number": 1}),
        ("search_pull_requests", {"owner": OWNER, "repo": REPOSITORY, "query": "is:open author:someone"}),
    ],
)
def test_fixed_repository_matches_its_configured_pair_case_insensitively(
    tool_name: str, arguments: dict[str, object]
) -> None:
    decision = _fixed(tool_name, arguments)
    assert isinstance(decision, RepositoryMatch)
    assert decision.target.names(OWNER, REPOSITORY)
    assert decision.confirmed_public is False
    assert decision.explanation == f"reviewed read targets repository {OWNER}/{REPOSITORY}"


def test_fixed_repository_code_search_takes_its_repository_from_the_one_qualifier() -> None:
    decision = _fixed("search_code", {"query": f"repo:{OWNER}/{REPOSITORY} language:python authorization"})
    assert decision == RepositoryMatch(
        target=TargetRepository(owner=OWNER, repository=REPOSITORY),
        confirmed_public=False,
        explanation=f"reviewed code search targets repository {OWNER}/{REPOSITORY}",
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "reason"),
    [
        ("issue_read", {"owner": OWNER, "repo": "other", "issue_number": 1}, "is outside"),
        ("get_job_logs", {"owner": "someone", "repo": REPOSITORY, "run_id": 7}, "is outside"),
        ("get_file_contents", {"owner": OWNER, "path": "README.md"}, "string owner/repo"),
        ("get_file_contents", {"owner": OWNER, "repo": 7, "path": "README.md"}, "string owner/repo"),
        ("search_pull_requests", {"owner": OWNER, "repo": REPOSITORY}, "string query"),
        ("search_pull_requests", {"owner": OWNER, "repo": REPOSITORY, "query": "repo:x/y is:open"}, "qualifier"),
        ("search_pull_requests", {"owner": OWNER, "repo": REPOSITORY, "query": "-repo:x/y is:open"}, "qualifier"),
        ("search_pull_requests", {"owner": OWNER, "repo": REPOSITORY, "query": "Repo:x/y is:open"}, "qualifier"),
        ("search_code", {}, "string query"),
        ("search_code", {"query": "authorization"}, "exactly one repository qualifier"),
        ("search_code", {"query": f"repo:{OWNER}/{REPOSITORY} repo:x/y auth"}, "exactly one repository qualifier"),
        ("search_code", {"query": f'"repo:{OWNER}/{REPOSITORY}" auth'}, "unquoted repo:owner/repo"),
        ("search_code", {"query": f"-repo:{OWNER}/{REPOSITORY} auth"}, "unquoted repo:owner/repo"),
        ("search_code", {"query": "repo:x/y auth"}, "is outside"),
    ],
)
def test_fixed_repository_mismatches(tool_name: str, arguments: dict[str, object], reason: str) -> None:
    decision = _fixed(tool_name, arguments)
    assert isinstance(decision, RepositoryMismatch)
    assert reason in decision.reason


def _visibility(*public: tuple[str, str], unavailable: bool = False) -> RepositoryVisibilityService:
    """Stands in for GitHub's unauthenticated repository endpoint: 200 for `public`, else 404."""
    confirmed = {(owner.casefold(), repository.casefold()) for owner, repository in public}

    def handle(request: httpx.Request) -> httpx.Response:
        if unavailable:
            return httpx.Response(500)
        _, _, owner, repository = request.url.path.split("/", 3)
        if (owner.casefold(), repository.casefold()) in confirmed:
            return httpx.Response(200, json={"private": False})
        return httpx.Response(404)

    http_client = httpx.AsyncClient(base_url="https://github-api.test", transport=httpx.MockTransport(handler=handle))
    return RepositoryVisibilityService(http_client, ttl_seconds=3600.0)


async def test_public_repository_matches_a_confirmed_public_target() -> None:
    decision = await evaluate_public_repository(
        "get_file_contents", {"owner": OWNER, "repo": REPOSITORY, "path": "README.md"}, _visibility((OWNER, REPOSITORY))
    )
    assert decision == RepositoryMatch(
        target=TargetRepository(owner=OWNER, repository=REPOSITORY),
        confirmed_public=True,
        explanation=f"reviewed read targets confirmed-public repository {OWNER}/{REPOSITORY}",
    )


async def test_public_repository_code_search_confirms_the_qualifier_repository() -> None:
    decision = await evaluate_public_repository(
        "search_code", {"query": f"repo:{OWNER}/{REPOSITORY} language:python"}, _visibility((OWNER, REPOSITORY))
    )
    assert isinstance(decision, RepositoryMatch)
    assert decision.explanation == f"reviewed code search targets confirmed-public repository {OWNER}/{REPOSITORY}"


async def test_public_repository_refuses_an_unconfirmed_target() -> None:
    decision = await evaluate_public_repository(
        "issue_read", {"owner": "someone", "repo": "private-thing", "issue_number": 1}, _visibility()
    )
    assert decision == RepositoryMismatch("repository someone/private-thing is not confirmed public")


async def test_public_repository_fails_closed_when_the_check_is_unavailable() -> None:
    decision = await evaluate_public_repository(
        "get_file_contents",
        {"owner": OWNER, "repo": REPOSITORY, "path": "README.md"},
        _visibility((OWNER, REPOSITORY), unavailable=True),
    )
    assert decision == RepositoryMismatch(f"could not confirm {OWNER}/{REPOSITORY} is public")


async def test_public_repository_rejects_a_smuggled_qualifier_before_looking_up_anything() -> None:
    """owner/repo names a confirmed-public repository, but the query's own `repo:` would target another."""
    decision = await evaluate_public_repository(
        "search_pull_requests",
        {"owner": OWNER, "repo": REPOSITORY, "query": "repo:someone/private-thing is:open"},
        _visibility((OWNER, REPOSITORY)),
    )
    assert isinstance(decision, RepositoryMismatch)
    assert "repository qualifier" in decision.reason


if __name__ == "__main__":
    pytest_bazel.main()
