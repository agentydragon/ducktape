from __future__ import annotations

from typing import Any

import pytest_bazel

from devinfra.ci.pr_mergeability import (
    Assessment,
    SourceRevisionError,
    inspect_pull_request,
    publish_check,
    verify_source_revision,
)


def _pull(*, mergeable: bool | None, head: str = "head", base: str = "base", merge: str = "merge") -> dict[str, Any]:
    return {
        "base": {"ref": "devel", "sha": base},
        "head": {"sha": head},
        "mergeable": mergeable,
        "mergeable_state": "dirty" if mergeable is False else "clean",
        "merge_commit_sha": merge,
    }


class FakeApi:
    def __init__(
        self, pulls: list[dict[str, Any]], commits: dict[str, dict[str, Any]], *, live_base: str = "base"
    ) -> None:
        self.pulls = pulls
        self.commits = commits
        self.live_base = live_base
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

    def get_git_ref(self, ref: str) -> dict[str, Any]:
        assert ref == "devel"
        return {"object": {"sha": self.live_base}}

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


def test_live_base_ref_accepts_current_merge_when_pr_base_metadata_lags() -> None:
    api = FakeApi(
        [_pull(mergeable=True, base="old-base")],
        {"merge": {"parents": [{"sha": "live-base"}, {"sha": "head"}]}},
        live_base="live-base",
    )

    result = inspect_pull_request(api, number=12)

    assert result.conclusion == "success"
    assert "Current base: `live-base`" in result.summary
    assert "PR base metadata: `old-base`" in result.summary


def test_lagged_pr_base_metadata_does_not_accept_a_stale_merge() -> None:
    api = FakeApi(
        [_pull(mergeable=True, base="old-base")],
        {"merge": {"parents": [{"sha": "old-base"}, {"sha": "head"}]}},
        live_base="live-base",
    )

    result = inspect_pull_request(api, number=12, attempts=1)

    assert result.conclusion == "failure"
    assert "expected live `devel` ref `live-base`" in result.summary


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


def test_source_ref_checkout_validates_the_synthetic_merge_head_parent() -> None:
    resolved_sha = verify_source_revision(
        "merge",
        source_ref="refs/pull/15/merge",
        expected_sha="event-sha",
        expected_head_sha="head",
        second_parent_sha="head",
    )

    assert resolved_sha == "merge"


def test_source_commit_checkout_must_match_the_requested_sha() -> None:
    message = _source_revision_error("checked-out", source_ref="", expected_sha="requested", expected_head_sha="")

    assert message == "checked out checked-out, expected requested"


def test_live_source_ref_mismatch_explains_that_a_fresh_workflow_is_needed() -> None:
    message = _source_revision_error(
        "merge",
        source_ref="refs/pull/15/merge",
        expected_sha="event-sha",
        expected_head_sha="current-head",
        second_parent_sha="old-head",
    )

    assert "merge parent old-head" in message
    assert "event-time PR head" in message
    assert "start a fresh PR workflow" in message


def test_event_merge_sha_mismatch_explains_that_rerun_reuses_source() -> None:
    message = _source_revision_error(
        "merge", source_ref="", expected_sha="merge", expected_head_sha="current-head", second_parent_sha="old-head"
    )

    assert "merge parent old-head" in message
    assert "rerunning this run reuses the old source" in message


def _source_revision_error(resolved_sha: str, **kwargs: str) -> str:
    try:
        verify_source_revision(resolved_sha, **kwargs)
    except SourceRevisionError as error:
        return str(error)
    raise AssertionError("invalid source revision was accepted")


if __name__ == "__main__":
    pytest_bazel.main()
