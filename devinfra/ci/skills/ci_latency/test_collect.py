import datetime as dt

import pytest

from devinfra.ci.skills.ci_latency.scripts import collect


class RunApi:
    def __init__(self, runs):
        self.runs = runs

    def request(self, endpoint, params, *, paginate):
        assert endpoint.endswith("/actions/runs")
        start, end = params["created"].split("..")
        first = dt.datetime.fromisoformat(start)
        last = dt.datetime.fromisoformat(end)
        matching = [run for run in self.runs if first <= dt.datetime.fromisoformat(run["created_at"]) <= last]
        if not paginate:
            return {"total_count": len(matching)}
        return [{"total_count": len(matching), "workflow_runs": matching}]


def test_run_collection_partitions_above_api_cap_without_gaps_or_overlap():
    runs = [{"id": run_id, "created_at": f"2026-01-01T00:00:0{(run_id - 1) // 550}Z"} for run_id in range(1, 1101)]

    result = collect.collect_runs(RunApi(runs), "owner/repo", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")

    assert len(result) == 1100
    assert {run["id"] for run in result} == set(range(1, 1101))


def test_run_collection_rejects_pagination_that_does_not_match_reported_total():
    class TruncatedApi(RunApi):
        def request(self, endpoint, params, *, paginate):
            if not paginate:
                return {"total_count": 2}
            return [{"total_count": 2, "workflow_runs": [{"id": 1}]}]

    with pytest.raises(ValueError, match="expected 2"):
        collect.collect_runs(TruncatedApi([]), "owner/repo", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")


def test_run_collection_fails_explicitly_when_one_second_exceeds_api_cap():
    runs = [{"id": run_id, "created_at": "2026-01-01T00:00:00Z"} for run_id in range(1000)]

    with pytest.raises(ValueError, match="one second"):
        collect.collect_runs(RunApi(runs), "owner/repo", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")


def test_active_old_run_is_kept_and_replaces_stale_window_state():
    old = {"id": 7, "created_at": "2020-01-01T00:00:00Z", "status": "completed"}
    refreshed = {"id": 7, "created_at": "2020-01-01T00:00:00Z", "status": "in_progress"}
    in_window = {"id": 8, "created_at": "2026-01-01T00:00:00Z", "status": "completed"}

    result = collect.combine_runs([old, in_window], {"in_progress": [refreshed]})

    assert result == [refreshed, in_window]


def test_job_collection_keeps_each_attempt_and_checks_pagination_total():
    class JobsApi:
        def request(self, endpoint, params, *, paginate):
            assert params["filter"] == "all"
            return [{"total_count": 2, "jobs": [{"id": 10, "run_attempt": 1}, {"id": 11, "run_attempt": 2}]}]

    result = collect._paginated_collection(
        JobsApi(), "repos/owner/repo/actions/runs/1/jobs", {"filter": "all", "per_page": "100"}, rows_key="jobs"
    )

    assert [job["run_attempt"] for job in result] == [1, 2]


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
