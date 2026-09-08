import json

import pytest
import pytest_bazel

from skills.forgejo.scripts import forgejo
from skills.forgejo.scripts.forgejo import Task

# ── timing ───────────────────────────────────────────────────────────────────


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
    task = Task.model_validate({**_row(run_started_at=None), "started_at": "2026-07-20T08:00:00Z"})
    assert task.run_started_at is None
    assert task.duration_seconds is None


def test_duration_is_updated_minus_run_started_for_finished() -> None:
    assert Task.model_validate(_row()).duration_seconds == 500.0


def test_unfinished_and_cancelled_have_no_duration() -> None:
    assert Task.model_validate(_row(status="running")).duration_seconds is None
    # cancelled carries a timestamp span but is not a real run time.
    assert Task.model_validate(_row(status="cancelled")).duration_seconds is None


def test_negative_span_is_rejected() -> None:
    assert Task.model_validate(_row(updated_at="2026-07-20T07:59:00Z")).duration_seconds is None


def test_recent_finished_sorts_by_id_desc_and_drops_unfinished() -> None:
    rows = [_row(id=10, status="running"), _row(id=9), _row(id=8), _row(id=7)]
    tasks = forgejo.recent_finished(rows, limit=2)
    # id=10 is newest but unfinished (dropped); the two most-recent finished are 9 and 8.
    assert [t.id for t in tasks] == [9, 8]


def test_summarize_groups_by_job_and_reports_dropped_outliers() -> None:
    tasks = [
        Task.model_validate(_row(id=1, name="bazel", updated_at="2026-07-20T08:08:20Z")),  # 500s
        Task.model_validate(_row(id=2, name="bazel", updated_at="2026-07-20T08:10:00Z")),  # 600s
        Task.model_validate(_row(id=3, name="validate", updated_at="2026-07-20T08:03:40Z")),  # 220s
        Task.model_validate(_row(id=4, name="bazel", updated_at="2026-07-20T09:00:00Z")),  # 3600s outlier
    ]
    stats, dropped = forgejo.summarize(tasks, max_seconds=1800.0)
    assert dropped == 1
    by_name = {s.name: s for s in stats}
    assert by_name["bazel"].n == 2  # the 3600s row was dropped
    assert by_name["bazel"].p_min == 500.0
    assert by_name["bazel"].p_max == 600.0
    assert by_name["validate"].n == 1


# ── logs ─────────────────────────────────────────────────────────────────────

RUN_HTML = """
<html>
  <body>
    <div
      data-actions-url="/haku/haku-state/actions"
      data-run-index="391"
      data-run-id="1883"
      data-job-index="0"
      data-attempt-number="1"
      data-initial-post-response='{"state":{"run":{"link":"/haku/haku-state/actions/runs/391","status":"failure","canRerun":true,"jobs":[{"id":4001,"name":"validate","status":"success","canRerun":true},{"id":4002,"name":"image","status":"failure","canRerun":true}]},"currentJob":{"steps":[{"index":8,"name":"Log in to the Forgejo registry","status":"failure"}]}}}'
    ></div>
  </body>
</html>
"""


def test_parse_run_page_extracts_web_ui_endpoint_state() -> None:
    state = forgejo.parse_run_page(RUN_HTML)

    assert state.actions_url == "/haku/haku-state/actions"
    assert state.run_index == "391"
    assert state.job_index == "0"
    assert state.attempt == "1"
    assert (
        state.log_endpoint("https://git.allegedly.works/")
        == "https://git.allegedly.works/haku/haku-state/actions/runs/391/jobs/0/attempt/1"
    )


def test_extract_steps_prefers_current_job_steps() -> None:
    state = forgejo.parse_run_page(RUN_HTML)

    assert forgejo.extract_steps(state.initial_post_response) == [
        {"index": 8, "name": "Log in to the Forgejo registry", "status": "failure"}
    ]


def test_print_steps_falls_back_to_position_and_summary(capsys: pytest.CaptureFixture[str]) -> None:
    forgejo._print_steps(
        [
            {"duration": "4s", "status": "success", "summary": "Set up job"},
            {"duration": "3m3s", "status": "failure", "summary": "Test"},
            {"index": 10, "status": "failure", "summary": "Complete job"},
        ]
    )

    assert capsys.readouterr().out.splitlines() == [
        "0\tsuccess\tSet up job",
        "1\tfailure\tTest",
        "10\tfailure\tComplete job",
    ]


def test_parse_run_page_unescapes_initial_post_response() -> None:
    escaped = RUN_HTML.replace('"state"', "&quot;state&quot;")

    state = forgejo.parse_run_page(escaped)

    assert forgejo.extract_steps(state.initial_post_response)[0]["index"] == 8


def test_parse_run_page_reports_missing_attrs() -> None:
    with pytest.raises(ValueError, match="data-actions-url"):
        forgejo.parse_run_page("<html></html>")


def test_build_log_payload_uses_step_cursor_shape() -> None:
    assert forgejo.build_log_payload(8) == {"logCursors": [{"step": 8, "cursor": None, "expanded": True}]}


def test_parse_log_response_handles_forgejo_lines() -> None:
    response = {
        "logs": {
            "stepsLog": [
                {
                    "lines": [
                        {
                            "timestamp": "2026-07-05T23:43:24Z",
                            "message": 'Error response from daemon: Get "https://git.allegedly.works/v2/": denied: Access denied',
                        }
                    ]
                }
            ]
        }
    }

    lines = forgejo.parse_log_response(json.dumps(response))

    assert lines == [
        forgejo.LogLine(
            timestamp="2026-07-05T23:43:24Z",
            message='Error response from daemon: Get "https://git.allegedly.works/v2/": denied: Access denied',
        )
    ]


# ── rerun ────────────────────────────────────────────────────────────────────


def test_run_view_lists_jobs_in_route_index_order() -> None:
    run = forgejo.run_view(forgejo.parse_run_page(RUN_HTML).initial_post_response)

    assert run.link == "/haku/haku-state/actions/runs/391"
    assert run.can_rerun
    assert [(job.name, job.status, job.can_rerun) for job in run.jobs] == [
        ("validate", "success", True),
        ("image", "failure", True),
    ]


def test_resolve_job_accepts_index_or_unique_name() -> None:
    jobs = forgejo.run_view(forgejo.parse_run_page(RUN_HTML).initial_post_response).jobs

    assert forgejo.resolve_job(jobs, "image") == 1
    assert forgejo.resolve_job(jobs, "0") == 0
    with pytest.raises(SystemExit, match="matches 0"):
        forgejo.resolve_job(jobs, "deploy")
    with pytest.raises(SystemExit, match="out of range"):
        forgejo.resolve_job(jobs, "2")


def test_rerun_endpoint_posts_under_the_page_run_link() -> None:
    link = "/haku/haku-state/actions/runs/391"

    assert forgejo.rerun_endpoint("https://forgejo.test/", link, None) == (
        "https://forgejo.test/haku/haku-state/actions/runs/391/rerun"
    )
    assert forgejo.rerun_endpoint("https://forgejo.test", link, 1) == (
        "https://forgejo.test/haku/haku-state/actions/runs/391/jobs/1/rerun"
    )


def test_credentials_prefer_flags_then_netrc(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    netrc_file = tmp_path / ".netrc"
    netrc_file.write_text("machine forgejo.test login bot password hunter2\n")
    netrc_file.chmod(0o600)

    assert forgejo.credentials("https://forgejo.test/", "flag-user", "flag-pass") == ("flag-user", "flag-pass")
    # A user without a password is not a credential; the netrc entry still wins.
    assert forgejo.credentials("https://forgejo.test/", "flag-user", None) == ("bot", "hunter2")
    with pytest.raises(SystemExit, match=r"no credentials for other\.test"):
        forgejo.credentials("https://other.test/", None, None)


if __name__ == "__main__":
    pytest_bazel.main()
