import json

import pytest
import pytest_bazel

from skills.forgejo.scripts import fetch_forgejo_logs

RUN_HTML = """
<html>
  <body>
    <div
      data-actions-url="/haku/haku-state/actions"
      data-run-index="391"
      data-run-id="1883"
      data-job-index="0"
      data-attempt-number="1"
      data-initial-post-response='{"state":{"currentJob":{"steps":[{"index":8,"name":"Log in to the Forgejo registry","status":"failure"}]}}}'
    ></div>
  </body>
</html>
"""


def test_parse_run_page_extracts_web_ui_endpoint_state() -> None:
    state = fetch_forgejo_logs.parse_run_page(RUN_HTML)

    assert state.actions_url == "/haku/haku-state/actions"
    assert state.run_index == "391"
    assert state.job_index == "0"
    assert state.attempt == "1"
    assert (
        state.log_endpoint("https://git.allegedly.works/")
        == "https://git.allegedly.works/haku/haku-state/actions/runs/391/jobs/0/attempt/1"
    )


def test_extract_steps_prefers_current_job_steps() -> None:
    state = fetch_forgejo_logs.parse_run_page(RUN_HTML)

    assert fetch_forgejo_logs.extract_steps(state.initial_post_response) == [
        {"index": 8, "name": "Log in to the Forgejo registry", "status": "failure"}
    ]


def test_parse_run_page_unescapes_initial_post_response() -> None:
    escaped = RUN_HTML.replace('"state"', "&quot;state&quot;")

    state = fetch_forgejo_logs.parse_run_page(escaped)

    assert fetch_forgejo_logs.extract_steps(state.initial_post_response)[0]["index"] == 8


def test_parse_run_page_reports_missing_attrs() -> None:
    with pytest.raises(ValueError, match="data-actions-url"):
        fetch_forgejo_logs.parse_run_page("<html></html>")


def test_build_log_payload_uses_step_cursor_shape() -> None:
    assert fetch_forgejo_logs.build_log_payload(8) == {"logCursors": [{"step": 8, "cursor": None, "expanded": True}]}


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

    lines = fetch_forgejo_logs.parse_log_response(json.dumps(response))

    assert lines == [
        fetch_forgejo_logs.LogLine(
            timestamp="2026-07-05T23:43:24Z",
            message='Error response from daemon: Get "https://git.allegedly.works/v2/": denied: Access denied',
        )
    ]


def test_parse_csrf_returns_blank_when_login_page_has_no_token() -> None:
    assert fetch_forgejo_logs.parse_csrf("<html></html>") == ""


if __name__ == "__main__":
    pytest_bazel.main()
