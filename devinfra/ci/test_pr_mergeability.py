from __future__ import annotations

from typing import Any

import pytest_bazel

from devinfra.ci.pr_mergeability import Assessment, inspect_pull_request, publish_check


def _pull(*, mergeable: bool | None, head: str = "head", base: str = "base", merge: str = "merge") -> dict[str, Any]:
    return {
        "base": {"ref": "devel", "sha": base},
        "head": {"sha": head},
        "mergeable": mergeable,
        "mergeable_state": "dirty" if mergeable is False else "clean",
        "merge_commit_sha": merge,
    }


class FakeApi:
    def __init__(self, pulls: list[dict[str, Any]], commits: dict[str, dict[str, Any]]) -> None:
        self.pulls = pulls
        self.commits = commits
        self.pull_calls = 0
        self.check_runs: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[int, dict[str, Any]]] = []

    def get_pull_request(self, _number: int) -> dict[str, Any]:
        result = self.pulls[min(self.pull_calls, len(self.pulls) - 1)]
        self.pull_calls += 1
        return result

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        return self.commits[sha]

    def get_check_runs(self, _sha: str) -> list[dict[str, Any]]:
        return self.check_runs

    def create_check_run(self, payload: dict[str, Any]) -> None:
        self.created.append(payload)

    def update_check_run(self, check_run_id: int, payload: dict[str, Any]) -> None:
        self.updated.append((check_run_id, payload))


def test_conflicted_pull_request_is_classified_as_source_failure() -> None:
    api = FakeApi([_pull(mergeable=False)], {})
    delays: list[float] = []

    result = inspect_pull_request(api, number=8507, sleep=delays.append)

    assert result.conclusion == "failure"
    assert result.title == "PR #8507 conflicts with devel"
    assert "Rerunning this CI run will reuse its recorded merge source" in result.summary
    assert not result.mergeable
    assert api.pull_calls == 1
    assert not delays


def test_clean_synthetic_merge_requires_current_base_and_head_parents() -> None:
    api = FakeApi([_pull(mergeable=True)], {"merge": {"parents": [{"sha": "base"}, {"sha": "head"}]}})

    result = inspect_pull_request(api, number=12)

    assert result.conclusion == "success"
    assert result.mergeable
    assert "expected parents" in result.summary


def test_stale_merge_commit_is_retried_after_github_reports_mergeable() -> None:
    api = FakeApi(
        [_pull(mergeable=True), _pull(mergeable=True, head="new-head")],
        {
            "merge": {"parents": [{"sha": "base"}, {"sha": "old-head"}]},
            "new-merge": {"parents": [{"sha": "base"}, {"sha": "new-head"}]},
        },
    )
    api.pulls[1]["merge_commit_sha"] = "new-merge"
    delays: list[float] = []

    result = inspect_pull_request(api, number=13, sleep=delays.append)

    assert result.conclusion == "success"
    assert result.head_sha == "new-head"
    assert api.pull_calls == 2
    assert delays == [1]


def test_unresolved_mergeability_fails_with_github_calculation_message() -> None:
    api = FakeApi([_pull(mergeable=None)], {})
    delays: list[float] = []

    result = inspect_pull_request(api, number=14, attempts=3, sleep=delays.append)

    assert result.conclusion == "failure"
    assert result.title == "GitHub mergeability is still unknown"
    assert "source-status delay" in result.summary
    assert api.pull_calls == 3
    assert delays == [1, 2]


def test_publish_check_creates_or_updates_the_pr_head_check() -> None:
    result = Assessment("head", "success", "Mergeable", "current merge is verified")
    api = FakeApi([], {})

    publish_check(api, number=15, result=result, details_url="https://example.test/run")

    assert api.created == [
        {
            "head_sha": "head",
            "name": "PR mergeability",
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "Mergeable", "summary": "current merge is verified"},
            "details_url": "https://example.test/run",
            "external_id": "pr-mergeability:15",
        }
    ]

    api.check_runs = [{"id": 7, "external_id": "pr-mergeability:15"}]
    publish_check(api, number=15, result=result, details_url="https://example.test/run")

    expected_update = api.created[0].copy()
    del expected_update["head_sha"]
    assert api.updated == [(7, expected_update)]


if __name__ == "__main__":
    pytest_bazel.main()
