import pytest

from devinfra.ci.skills.ci_latency.scripts import snapshot_analysis


def _run(run_id, workflow_id, created, sha, *, head_repo="owner/repo", attempt=1, event="pull_request"):
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "path": f".github/workflows/{workflow_id}.yml",
        "created_at": created,
        "event": event,
        "head_sha": sha,
        "repository": {"full_name": "owner/repo"},
        "head_repository": {"full_name": head_repo},
        "run_attempt": attempt,
        "pull_requests": [],
    }


def _job(job_id, run_id, name, created, started, completed, *, attempt=1, conclusion="success", status="completed"):
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "runner_id": 4,
    }


def test_feedback_groups_by_exact_head_and_uses_each_latest_gate_attempt_clock():
    build = "Build gate"
    precommit = "Pre-commit gate"
    head = "a" * 40
    snapshot = {
        "repository": "owner/repo",
        "runs": [
            _run(1, 10, "2026-01-01T10:00:00Z", head, attempt=2),
            _run(2, 20, "2026-01-01T10:00:30Z", head),
            _run(3, 10, "2026-01-01T11:00:00Z", "b" * 40),
            _run(4, 20, "2026-01-01T11:00:00Z", "b" * 40),
        ],
        "jobs": [
            _job(11, 1, build, "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z", "2026-01-01T10:02:00Z"),
            _job(12, 1, build, "2026-01-01T10:00:00Z", "2026-01-01T10:02:00Z", "2026-01-01T10:04:00Z", attempt=2),
            _job(21, 2, precommit, "2026-01-01T10:00:35Z", "2026-01-01T10:01:00Z", "2026-01-01T10:03:00Z"),
            _job(31, 3, build, "2026-01-01T11:00:00Z", "2026-01-01T11:01:00Z", "2026-01-01T11:02:00Z", conclusion="cancelled"),
            _job(41, 4, precommit, "2026-01-01T11:00:00Z", "2026-01-01T11:00:30Z", "2026-01-01T11:01:00Z"),
        ],
    }

    result = snapshot_analysis.analyze_feedback(
        snapshot,
        "2026-01-01T09:00:00Z",
        "2026-01-01T12:00:00Z",
        [build, precommit],
    )

    assert result["outcomes"] == {"green-proxy": 1, "canceled": 1}
    first, second = result["heads"]
    assert first["head_sha"] == head
    assert first["feedback_proxy_seconds"] == 240
    assert first["gates"][0]["workflows"][0]["run_attempt"] == 2
    assert first["gates"][0]["workflows"][0]["elapsed_seconds"] == 240
    assert second["outcome"] == "canceled"
    assert second["gates"][0]["state"] == "canceled"


def test_latest_attempt_without_visible_gate_does_not_reuse_older_success():
    gate = "Required gate"
    head = "c" * 40
    snapshot = {
        "runs": [_run(1, 10, "2026-01-01T10:00:00Z", head, attempt=2)],
        "jobs": [_job(1, 1, gate, "2026-01-01T10:00:00Z", "2026-01-01T10:00:30Z", "2026-01-01T10:01:00Z", attempt=1)],
    }

    result = snapshot_analysis.analyze_feedback(
        snapshot, "2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z", [gate]
    )

    assert result["heads"][0]["gates"][0]["state"] == "missing"
    assert result["heads"][0]["feedback_proxy_seconds"] is None


def test_fork_and_unknown_source_states_are_not_reported_as_green():
    gate_a, gate_b = "Gate A", "Gate B"
    fork = _run(1, 10, "2026-01-01T10:00:00Z", "d" * 40, head_repo="contributor/fork")
    unknown = _run(2, 10, "2026-01-01T10:01:00Z", "e" * 40)
    unknown.pop("head_repository")
    snapshot = {
        "runs": [fork, unknown],
        "jobs": [_job(1, 1, gate_a, "2026-01-01T10:00:00Z", "2026-01-01T10:00:30Z", "2026-01-01T10:01:00Z")],
    }

    result = snapshot_analysis.analyze_feedback(
        snapshot, "2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z", [gate_a, gate_b]
    )

    outcomes = {head["head_sha"]: head["outcome"] for head in result["heads"]}
    assert outcomes["d" * 40] == "fork-unlinked"
    assert outcomes["e" * 40] == "unknown-source"


def test_completed_runner_occupancy_is_clipped_and_uses_github_job_clocks_only():
    snapshot = {
        "runs": [
            {"id": 1, "path": "workflow-a.yml"},
            {"id": 2, "path": "workflow-b.yml"},
        ],
        "jobs": [
            {
                **_job(1, 1, "completed", "2026-01-01T09:59:58Z", "2026-01-01T09:59:58Z", "2026-01-01T10:00:05Z"),
                "buildbuddy_cached_duration_seconds": 999999,
            },
            _job(2, 2, "cancelled on runner", "2026-01-01T10:00:03Z", "2026-01-01T10:00:03Z", "2026-01-01T10:00:11Z", conclusion="cancelled"),
            _job(3, 1, "unfinished", "2026-01-01T10:00:00Z", "2026-01-01T10:00:02Z", None, status="in_progress"),
            {**_job(4, 1, "unassigned", "2026-01-01T10:00:00Z", "2026-01-01T10:00:01Z", "2026-01-01T10:00:03Z"), "runner_id": 0},
            {**_job(5, 1, "missing timestamp", "2026-01-01T10:00:00Z", None, "2026-01-01T10:00:03Z"), "runner_id": 4},
        ],
    }

    result = snapshot_analysis.analyze_occupancy(
        snapshot,
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:10Z",
        set(),
        [2],
    )

    assert result["runner_seconds_by_workflow"] == {"workflow-a.yml": 5, "workflow-b.yml": 7}
    assert result["runner_seconds_total"] == 12
    assert result["mean_busy_runners"] == pytest.approx(1.2)
    assert result["peak_busy_runners"] == 2
    assert result["minutes_at_least_threshold"]["2"] == pytest.approx(2 / 60)
    assert result["excluded_jobs"] == {
        "missing_timestamps": 1,
        "no_positive_runner_id": 1,
        "unfinished_or_unknown_status": 1,
    }


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
