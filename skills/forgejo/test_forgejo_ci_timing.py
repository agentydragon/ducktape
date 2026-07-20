import pytest_bazel

from skills.forgejo.scripts import forgejo_ci_timing
from skills.forgejo.scripts.forgejo_ci_timing import Task


def _row(**kw):
    base = {
        "id": 1,
        "name": "bazel",
        "status": "success",
        "run_started_at": "2026-07-20T08:00:00Z",
        "updated_at": "2026-07-20T08:08:20Z",
    }
    return {**base, **kw}


def test_from_row_reads_run_started_at_not_started_at() -> None:
    # The endpoint has no `started_at`; a row that only carries `started_at`
    # (the wrong field) must yield no duration, not a bogus one.
    task = Task.from_row({**_row(run_started_at=None), "started_at": "2026-07-20T08:00:00Z"})
    assert task.run_started_at is None
    assert task.duration_seconds is None


def test_duration_is_updated_minus_run_started_for_finished() -> None:
    assert Task.from_row(_row()).duration_seconds == 500.0


def test_unfinished_and_cancelled_have_no_duration() -> None:
    assert Task.from_row(_row(status="running")).duration_seconds is None
    # cancelled carries a timestamp span but is not a real run time.
    assert Task.from_row(_row(status="cancelled")).duration_seconds is None


def test_negative_span_is_rejected() -> None:
    assert Task.from_row(_row(updated_at="2026-07-20T07:59:00Z")).duration_seconds is None


def test_recent_finished_sorts_by_id_desc_and_drops_unfinished() -> None:
    rows = [_row(id=10, status="running"), _row(id=9), _row(id=8), _row(id=7)]
    tasks = forgejo_ci_timing.recent_finished(rows, limit=2)
    # id=10 is newest but unfinished (dropped); the two most-recent finished are 9 and 8.
    assert [t.id for t in tasks] == [9, 8]


def test_summarize_groups_by_job_and_reports_dropped_outliers() -> None:
    tasks = [
        Task.from_row(_row(id=1, name="bazel", updated_at="2026-07-20T08:08:20Z")),  # 500s
        Task.from_row(_row(id=2, name="bazel", updated_at="2026-07-20T08:10:00Z")),  # 600s
        Task.from_row(_row(id=3, name="validate", updated_at="2026-07-20T08:03:40Z")),  # 220s
        Task.from_row(_row(id=4, name="bazel", updated_at="2026-07-20T09:00:00Z")),  # 3600s outlier
    ]
    stats, dropped = forgejo_ci_timing.summarize(tasks, max_seconds=1800.0)
    assert dropped == 1
    by_name = {s.name: s for s in stats}
    assert by_name["bazel"].n == 2  # the 3600s row was dropped
    assert by_name["bazel"].p_min == 500.0
    assert by_name["bazel"].p_max == 600.0
    assert by_name["validate"].n == 1


if __name__ == "__main__":
    pytest_bazel.main()
