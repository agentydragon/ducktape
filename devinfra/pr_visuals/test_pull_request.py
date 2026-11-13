from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import pytest_bazel

from devinfra.pr_visuals.pull_request import PullRequestRef, find_reviewable_pull_request

HEAD_SHA = "a" * 40


def _pull(
    number: int, *, head_sha: str = HEAD_SHA, base_sha: str = "b" * 40, state: str = "open", merged: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        head=SimpleNamespace(sha=head_sha),
        base=SimpleNamespace(sha=base_sha),
        state=state,
        merged=merged,
    )


@dataclass
class FakeGithub:
    pulls: list[SimpleNamespace]
    queries: list[dict[str, str]] = field(default_factory=list)

    def __enter__(self) -> FakeGithub:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_repo(self, repository: str) -> FakeGithub:
        assert repository == "example/repo"
        return self

    def get_pulls(self, **query: str) -> list[SimpleNamespace]:
        self.queries.append(query)
        return self.pulls


def _find(pulls: list[SimpleNamespace]) -> tuple[PullRequestRef | None, FakeGithub]:
    github = FakeGithub(pulls)
    found = find_reviewable_pull_request(
        github=github, repository="example/repo", head="fork-owner:topic", head_sha=HEAD_SHA
    )
    return found, github


@pytest.mark.parametrize(("state", "merged"), [("open", False), ("closed", True)], ids=["open", "merged"])
def test_the_pr_whose_head_is_the_run_commit_is_found_by_its_head_ref(state: str, merged: bool) -> None:
    found, github = _find([_pull(8733, base_sha="c" * 40, state=state, merged=merged)])

    assert found == PullRequestRef(number=8733, base_sha="c" * 40)
    assert github.queries == [{"state": "all", "head": "fork-owner:topic"}]


@pytest.mark.parametrize(("state", "merged"), [("open", False), ("closed", True)], ids=["open", "merged"])
def test_a_pr_that_moved_on_to_a_newer_commit_is_not_the_runs_pr(state: str, merged: bool) -> None:
    found, _ = _find([_pull(8733, head_sha="d" * 40, state=state, merged=merged)])

    assert found is None


def test_a_closed_unmerged_pr_is_not_reviewable() -> None:
    found, _ = _find([_pull(8733, state="closed")])

    assert found is None


def test_two_prs_at_the_same_commit_are_ambiguous() -> None:
    with pytest.raises(ValueError, match="Expected exactly one"):
        _find([_pull(1), _pull(2)])


if __name__ == "__main__":
    pytest_bazel.main()
