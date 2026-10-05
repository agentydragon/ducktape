from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel

from devinfra.ci.pr_mergeability import (
    Conflicted,
    GitHubApi,
    GitHubApiError,
    MergeCommitUnavailableError,
    Obsolete,
    Resolved,
    SourceRevisionError,
    resolve_merge_commit,
    verify_source_revision,
)


def _pull(
    *, mergeable: bool | None = True, head: str = "head", state: str = "open", merge: str = "merge"
) -> dict[str, Any]:
    return {
        "state": state,
        "base": {"ref": "devel"},
        "head": {"sha": head},
        "mergeable": mergeable,
        "mergeable_state": "dirty" if mergeable is False else "clean",
        "merge_commit_sha": merge,
    }


def _merge(*parents: str) -> dict[str, Any]:
    return {"parents": [{"sha": sha} for sha in parents]}


class FakeApi(GitHubApi):
    """Serves ``pulls`` in order, repeating the last; an error entry is raised instead of returned."""

    def __init__(self, pulls: list[dict[str, Any] | GitHubApiError], commits: dict[str, dict[str, Any]]) -> None:
        super().__init__(repository="example/repo", token="test-token")
        self.pulls = pulls
        self.commits = commits
        self.pull_calls = 0

    def get_pull_request(self, number: int) -> dict[str, Any]:
        pull = self.pulls[min(self.pull_calls, len(self.pulls) - 1)]
        self.pull_calls += 1
        if isinstance(pull, GitHubApiError):
            raise pull
        return pull

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        return self.commits[sha]


def test_clean_merge_of_the_pr_head_resolves_at_once() -> None:
    api = FakeApi([_pull()], {"merge": _merge("base", "head")})
    delays: list[float] = []

    assert resolve_merge_commit(api, number=12, head_sha="head", sleep=delays.append) == Resolved("merge")
    assert api.pull_calls == 1
    assert not delays


def test_merge_built_on_an_older_base_resolves() -> None:
    # The first parent is whichever commit GitHub merged onto; it is not compared with the live base.
    api = FakeApi([_pull()], {"merge": _merge("older-base", "head")})

    assert resolve_merge_commit(api, number=12, head_sha="head") == Resolved("merge")


def test_waits_while_github_calculates_the_merge() -> None:
    api = FakeApi([_pull(mergeable=None), _pull(mergeable=None), _pull()], {"merge": _merge("base", "head")})
    delays: list[float] = []

    outcome = resolve_merge_commit(api, number=12, head_sha="head", retry_delays=(1, 2, 4), sleep=delays.append)

    assert outcome == Resolved("merge")
    assert delays == [1, 2]


def test_waits_until_the_merge_commit_has_the_current_head() -> None:
    api = FakeApi(
        [_pull(merge="stale-merge"), _pull(merge="merge")],
        {"stale-merge": _merge("base", "old-head"), "merge": _merge("base", "head")},
    )
    delays: list[float] = []

    assert resolve_merge_commit(api, number=12, head_sha="head", sleep=delays.append) == Resolved("merge")
    assert delays == [1]


def test_retries_github_api_errors() -> None:
    api = FakeApi([GitHubApiError("HTTP 502"), _pull()], {"merge": _merge("base", "head")})
    delays: list[float] = []

    assert resolve_merge_commit(api, number=12, head_sha="head", sleep=delays.append) == Resolved("merge")
    assert delays == [1]


def test_conflict_is_reported_at_once() -> None:
    api = FakeApi([_pull(mergeable=False)], {})
    delays: list[float] = []

    outcome = resolve_merge_commit(api, number=12, head_sha="head", sleep=delays.append)

    assert isinstance(outcome, Conflicted)
    assert api.pull_calls == 1
    assert not delays


@pytest.mark.parametrize("pull", [_pull(head="newer-head"), _pull(state="closed")])
def test_moved_head_or_closed_pr_is_obsolete(pull: dict[str, Any]) -> None:
    assert isinstance(resolve_merge_commit(FakeApi([pull], {}), number=12, head_sha="head"), Obsolete)


def test_fails_loudly_when_github_never_builds_the_merge() -> None:
    api = FakeApi([_pull(mergeable=None)], {})
    delays: list[float] = []

    with pytest.raises(MergeCommitUnavailableError):
        resolve_merge_commit(api, number=12, head_sha="head", retry_delays=(1, 2), sleep=delays.append)

    assert delays == [1, 2]
    assert api.pull_calls == 3


def test_verified_source_is_the_resolved_commit_with_the_pr_head_as_second_parent() -> None:
    resolved = verify_source_revision("merge", expected_sha="merge", expected_head_sha="head", second_parent_sha="head")

    assert resolved == "merge"


def test_checkout_of_another_commit_is_rejected() -> None:
    with pytest.raises(SourceRevisionError):
        verify_source_revision("checked-out", expected_sha="requested", expected_head_sha="")


def test_merge_of_another_head_is_rejected() -> None:
    with pytest.raises(SourceRevisionError):
        verify_source_revision(
            "merge", expected_sha="merge", expected_head_sha="current-head", second_parent_sha="old-head"
        )


if __name__ == "__main__":
    pytest_bazel.main()
