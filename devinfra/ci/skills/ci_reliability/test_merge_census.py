from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from devinfra.ci.skills.ci_reliability.scripts import merge_census


class FakeGitHub:
    def __init__(self, rows_by_day: dict[str, list[dict[str, Any]]], reported: dict[str, int] | None = None):
        self.rows_by_day = rows_by_day
        self.reported = reported or {day: len(rows) for day, rows in rows_by_day.items()}

    def search_count(self, repo: str, day: str) -> int:
        assert repo == "owner/repo"
        return self.reported.get(day, 0)

    def list_merged_prs(self, repo: str, day: str) -> list[dict[str, Any]]:
        assert repo == "owner/repo"
        return self.rows_by_day.get(day, [])


def _check(name: str, started: str, completed: str | None, *, status: str, conclusion: str | None):
    return {
        "__typename": "CheckRun",
        "name": name,
        "startedAt": started,
        "completedAt": completed,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://example.test/check",
    }


def test_window_collection_partitions_days_and_filters_exact_merge_times():
    github = FakeGitHub(
        {
            "2026-01-01": [
                {"number": 1, "mergedAt": "2026-01-01T00:00:01Z"},
                {"number": 2, "mergedAt": "2026-01-01T23:59:59Z"},
            ],
            "2026-01-02": [{"number": 3, "mergedAt": "2026-01-02T00:00:01Z"}],
        }
    )

    rows, partitions = merge_census.collect_window(github, "owner/repo", "2026-01-01T23:00:00Z", "2026-01-02T01:00:00Z")

    assert [row["number"] for row in rows] == [2, 3]
    assert [partition["day_utc"] for partition in partitions] == ["2026-01-01", "2026-01-02"]
    assert all(partition["complete"] for partition in partitions)


def test_window_collection_rejects_capped_or_incomplete_daily_partitions():
    capped = FakeGitHub({}, {"2026-01-01": merge_census.SEARCH_LIMIT})
    with pytest.raises(ValueError, match="search cap"):
        merge_census.collect_window(capped, "owner/repo", "2026-01-01T00:00:00Z", "2026-01-01T23:59:59Z")

    incomplete = FakeGitHub({"2026-01-01": [{"number": 1, "mergedAt": "2026-01-01T01:00:00Z"}]}, {"2026-01-01": 2})
    with pytest.raises(ValueError, match="search reported 2"):
        merge_census.collect_window(incomplete, "owner/repo", "2026-01-01T00:00:00Z", "2026-01-01T23:59:59Z")


def test_search_count_rejects_github_search_incomplete_results(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        merge_census.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout='{"total_count": 1, "incomplete_results": true}', stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="incomplete results"):
        merge_census.GhCli().search_count("owner/repo", "2026-01-01")


def test_latest_visible_rerun_after_merge_is_unknown_and_history_limit_is_explicit():
    pr = {
        "number": 15,
        "url": "https://github.com/owner/repo/pull/15",
        "mergedAt": "2026-01-10T12:00:00Z",
        "headRefOid": "a" * 40,
        "statusCheckRollup": [
            _check(
                "Required gate",
                "2026-01-10T08:00:00Z",
                "2026-01-10T09:00:00Z",
                status="COMPLETED",
                conclusion="FAILURE",
            ),
            _check(
                "Required gate",
                "2026-01-10T10:00:00Z",
                "2026-01-10T11:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            ),
            _check(
                "Required gate",
                "2026-01-10T13:00:00Z",
                "2026-01-10T14:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            ),
        ],
    }

    result = merge_census.classify_pr(pr, ["Required gate"])
    assert result["overall"] == "latest_attempt_started_after_merge"
    assert result["gates"]["Required gate"]["at_merge"] == "unknown"
    assert (
        "superseded historical attempts are not reconstructed"
        in result["gates"]["Required gate"]["history_uncertainty"]
    )


def test_failed_pending_canceled_and_missing_gates_are_classified_as_candidates():
    merged_at = "2026-01-10T12:00:00Z"
    pr = {
        "mergedAt": merged_at,
        "statusCheckRollup": [
            _check("failed", "2026-01-10T10:00:00Z", "2026-01-10T11:00:00Z", status="COMPLETED", conclusion="FAILURE"),
            _check("pending", "2026-01-10T11:00:00Z", None, status="IN_PROGRESS", conclusion=None),
            _check(
                "canceled", "2026-01-10T10:00:00Z", "2026-01-10T11:00:00Z", status="COMPLETED", conclusion="CANCELLED"
            ),
        ],
    }

    assert merge_census.classify_gate(pr, "failed")["class"] == "visible_red_by_merge"
    assert merge_census.classify_gate(pr, "pending")["class"] == "latest_attempt_straddled_merge"
    assert merge_census.classify_gate(pr, "canceled")["class"] == "visible_cancelled_by_merge"
    assert merge_census.classify_gate(pr, "missing")["class"] == "missing_from_current_rollup"


def test_latest_visible_green_candidate_retains_historical_attempt_uncertainty():
    pr = {
        "mergedAt": "2026-01-10T12:00:00Z",
        "statusCheckRollup": [
            _check(
                "Required gate",
                "2026-01-10T08:00:00Z",
                "2026-01-10T09:00:00Z",
                status="COMPLETED",
                conclusion="FAILURE",
            ),
            _check(
                "Required gate",
                "2026-01-10T10:00:00Z",
                "2026-01-10T11:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            ),
        ],
    }

    result = merge_census.classify_gate(pr, "Required gate")
    assert result["class"] == "visible_green_by_merge"
    assert "superseded historical attempts are not reconstructed" in result["history_uncertainty"]


def test_sentinel_current_gate_attempt_is_unknown_instead_of_reusing_older_green():
    pr = {
        "mergedAt": "2026-01-10T12:00:00Z",
        "statusCheckRollup": [
            _check(
                "Required gate",
                "2026-01-10T10:00:00Z",
                "2026-01-10T11:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            ),
            _check("Required gate", "0001-01-01T00:00:00Z", None, status="IN_PROGRESS", conclusion=None),
        ],
    }

    result = merge_census.classify_gate(pr, "Required gate")

    assert result["class"] == "unclassifiable_current_rollup"
    assert result["at_merge"] == "unknown"


def test_completed_gate_with_end_before_start_is_unknown():
    pr = {
        "mergedAt": "2026-01-10T12:00:00Z",
        "statusCheckRollup": [
            _check(
                "Required gate",
                "2026-01-10T11:00:00Z",
                "2026-01-10T10:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            )
        ],
    }

    result = merge_census.classify_gate(pr, "Required gate")

    assert result["class"] == "unclassifiable_current_rollup"
    assert result["at_merge"] == "unknown"


def test_completed_gate_with_sentinel_completion_clock_is_unknown():
    pr = {
        "mergedAt": "2026-01-10T12:00:00Z",
        "statusCheckRollup": [
            _check(
                "Required gate",
                "2026-01-10T10:00:00Z",
                "0001-01-01T00:00:00Z",
                status="COMPLETED",
                conclusion="SUCCESS",
            )
        ],
    }

    result = merge_census.classify_gate(pr, "Required gate")

    assert result["class"] == "unclassifiable_current_rollup"
    assert result["at_merge"] == "unknown"


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
