"""Tests for publishing live BuildBuddy test status."""

from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci import live_test_status


@pytest.fixture
def status_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Namespace:
    monkeypatch.setattr(live_test_status, "STOP", Event())
    monkeypatch.setattr(live_test_status, "_credentials", lambda: ("bb-key", "gh-token", "owner/repo"))
    return Namespace(
        invocation_id="invocation-123",
        sha="abc123",
        state_file=tmp_path / "state.json",
        pid_file=tmp_path / "watcher.pid",
        buildbuddy_url="https://buildbuddy.example",
        github_api_url="https://github.example/api",
        poll_interval=0.0,
    )


def test_watch_publishes_failure_before_invocation_completes(
    monkeypatch: pytest.MonkeyPatch, status_args: Namespace
) -> None:
    publish_status = Mock()
    monkeypatch.setattr(
        live_test_status,
        "_invocation",
        lambda *_args: {"invocationStatus": "PARTIAL_INVOCATION_STATUS", "success": False},
    )
    monkeypatch.setattr(live_test_status, "_first_failed_test", lambda *_args: "//pkg:failed_test")
    monkeypatch.setattr(live_test_status, "_publish_status", publish_status)

    live_test_status._watch(status_args)

    publish_status.assert_called_once_with(
        repo="owner/repo",
        sha="abc123",
        token="gh-token",
        invocation_id="invocation-123",
        state="failure",
        description="First failing ordinary test: //pkg:failed_test",
        state_path=status_args.state_file,
        github_api_url="https://github.example/api",
        buildbuddy_url="https://buildbuddy.example",
    )


def test_watch_keeps_pending_when_invocation_disconnects(
    monkeypatch: pytest.MonkeyPatch, status_args: Namespace
) -> None:
    publish_status = Mock()
    statuses = iter(["DISCONNECTED_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    monkeypatch.setattr(
        live_test_status, "_invocation", lambda *_args: {"invocationStatus": next(statuses), "success": True}
    )
    monkeypatch.setattr(live_test_status, "_first_failed_test", lambda *_args: None)
    monkeypatch.setattr(live_test_status, "_publish_status", publish_status)

    live_test_status._watch(status_args)

    assert [call.kwargs["state"] for call in publish_status.call_args_list] == ["pending", "success"]


def test_first_failed_build_queries_the_exact_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Mock(
        return_value={
            "targetGroups": [
                {"status": "FAILED", "targets": [{"metadata": {"label": "//pkg:unrelated_test"}}]},
                {"status": "FAILED_TO_BUILD", "targets": [{"metadata": {"label": "//pkg:compile_target"}}]},
            ]
        }
    )
    monkeypatch.setattr(live_test_status, "_json_request", request)

    assert live_test_status._first_failed_build("key", "exact-invocation", "https://buildbuddy.example/") == (
        "//pkg:compile_target"
    )
    request.assert_called_once_with(
        "https://buildbuddy.example/rpc/BuildBuddyService/GetTarget",
        headers={"x-buildbuddy-api-key": "key", "Content-Type": "application/json"},
        body={"invocationId": "exact-invocation", "status": "FAILED_TO_BUILD"},
        timeout=3,
    )


def test_watch_keeps_build_failure_pending_until_final_job(
    monkeypatch: pytest.MonkeyPatch, status_args: Namespace
) -> None:
    publish_status = Mock(return_value=True)
    failed_build = Mock(return_value="//pkg:compile_target")
    statuses = iter(["PARTIAL_INVOCATION_STATUS", "PARTIAL_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS"])
    monkeypatch.setattr(live_test_status, "_invocation", lambda *_args: {"invocationStatus": next(statuses)})
    monkeypatch.setattr(live_test_status, "_first_failed_test", lambda *_args: None)
    monkeypatch.setattr(live_test_status, "_first_failed_build", failed_build)
    monkeypatch.setattr(live_test_status, "_publish_status", publish_status)

    live_test_status._watch(status_args)

    failed_build.assert_called_once()
    publish_status.assert_called_once()
    assert publish_status.call_args.kwargs["state"] == "pending"
    assert publish_status.call_args.kwargs["description"] == "Bazel attempt failed to build: //pkg:compile_target"
    assert publish_status.call_args.kwargs["invocation_id"] == status_args.invocation_id


@pytest.mark.parametrize(
    ("outcome", "expected_state", "expected_description", "expected_url"),
    [
        (
            "success",
            "success",
            "Bazel CI passed; initial attempt failed to build",
            "https://github.com/owner/repo/actions/runs/123/attempts/2",
        ),
        ("failure", "failure", "Bazel attempt failed to build: //pkg:target", None),
        (
            "cancelled",
            "error",
            "Bazel CI ended (cancelled); see Test & Build job",
            "https://github.com/owner/repo/actions/runs/123/attempts/2",
        ),
    ],
)
def test_finish_supersedes_pending_build_failure(
    monkeypatch: pytest.MonkeyPatch,
    status_args: Namespace,
    outcome: str,
    expected_state: str,
    expected_description: str,
    expected_url: str | None,
) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(
        live_test_status, "_invocation", Mock(side_effect=AssertionError("unexpected invocation lookup"))
    )
    publish_status = Mock()
    monkeypatch.setattr(live_test_status, "_publish_status", publish_status)
    live_test_status._write_state(status_args.state_file, "pending", "Bazel attempt failed to build: //pkg:target")
    status_args.remote_outcome = outcome

    live_test_status._finish(status_args)

    publish_status.assert_called_once()
    assert publish_status.call_args.kwargs["state"] == expected_state
    assert publish_status.call_args.kwargs["description"] == expected_description
    assert publish_status.call_args.kwargs["target_url"] == expected_url


def _api_error(*_args: object) -> None:
    raise live_test_status.APIError("HTTP Error 500: Internal Server Error")


def test_finish_retries_status_after_github_rejects_watch_post(
    monkeypatch: pytest.MonkeyPatch, status_args: Namespace
) -> None:
    state_file = status_args.state_file
    live_test_status._write_state(state_file, "pending", "Watching BuildBuddy ordinary test invocation")
    post_status = Mock(side_effect=[live_test_status.APIError("HTTP Error 500"), {}])
    monkeypatch.setattr(live_test_status, "_json_request", post_status)
    monkeypatch.setattr(
        live_test_status,
        "_invocation",
        lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
    )
    status_args.remote_outcome = "failure"

    live_test_status._publish_status(
        repo="owner/repo",
        sha="abc123",
        token="gh-token",
        invocation_id="invocation-123",
        state="failure",
        description="Bazel ordinary test invocation failed",
        state_path=state_file,
        github_api_url=status_args.github_api_url,
        buildbuddy_url=status_args.buildbuddy_url,
    )
    assert live_test_status._read_state(state_file) == {
        "state": "pending",
        "description": "Watching BuildBuddy ordinary test invocation",
    }

    live_test_status._finish(status_args)

    assert post_status.call_count == 2
    assert live_test_status._read_state(state_file) == {
        "state": "failure",
        "description": "Bazel ordinary test invocation failed",
    }
    assert post_status.call_args.kwargs["body"]["context"] == "BuildBuddy / Bazel ordinary tests (live)"


@pytest.mark.parametrize(
    ("lookup", "remote_outcome", "expected_state"),
    [
        pytest.param(lambda *_args: None, "success", "success", id="no-invocation-ci-passed"),
        pytest.param(lambda *_args: None, "failure", "error", id="no-invocation-ci-failed"),
        pytest.param(_api_error, "success", "error", id="lookup-failed"),
        pytest.param(
            lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
            "failure",
            "failure",
            id="tests-failed",
        ),
        pytest.param(
            lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
            "cancelled",
            "failure",
            id="tests-failed-ci-cancelled",
        ),
        pytest.param(
            lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
            "skipped",
            "failure",
            id="tests-failed-ci-skipped",
        ),
        pytest.param(
            lambda *_args: {"invocationStatus": "DISCONNECTED_INVOCATION_STATUS"},
            "success",
            "success",
            id="disconnected-ci-passed",
        ),
        pytest.param(
            lambda *_args: {"invocationStatus": "DISCONNECTED_INVOCATION_STATUS"},
            "failure",
            "failure",
            id="disconnected-ci-failed",
        ),
        pytest.param(
            lambda *_args: {"invocationStatus": "DISCONNECTED_INVOCATION_STATUS"},
            "cancelled",
            "error",
            id="disconnected-ci-cancelled",
        ),
    ],
)
def test_finish_status(
    monkeypatch: pytest.MonkeyPatch,
    status_args: Namespace,
    lookup: Callable[..., dict[str, object] | None],
    remote_outcome: str,
    expected_state: str,
) -> None:
    status_args.remote_outcome = remote_outcome
    publish_status = Mock()
    monkeypatch.setattr(live_test_status, "_invocation", lookup)
    monkeypatch.setattr(live_test_status, "_publish_status", publish_status)

    live_test_status._finish(status_args)

    assert publish_status.call_args.kwargs["state"] == expected_state


def test_finish_successful_ci_replaces_early_failure_and_links_workflow(
    monkeypatch: pytest.MonkeyPatch, status_args: Namespace
) -> None:
    state_file = status_args.state_file
    live_test_status._write_state(state_file, "failure", "First failing ordinary test: //pkg:flaky_test")
    workflow_url = "https://github.com/owner/repo/actions/runs/123/attempts/2"
    post_status = Mock(return_value={})
    monkeypatch.setattr(live_test_status, "_json_request", post_status)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(
        live_test_status,
        "_invocation",
        lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
    )
    status_args.invocation_id = "failed-attempt"
    status_args.remote_outcome = "success"

    live_test_status._finish(status_args)

    body = post_status.call_args.kwargs["body"]
    assert body["state"] == "success"
    assert body["description"] == "Bazel CI passed; initial test invocation failed"
    assert body["target_url"] == workflow_url
    assert live_test_status._read_state(state_file) == {
        "state": "success",
        "description": "Bazel CI passed; initial test invocation failed",
    }


if __name__ == "__main__":
    pytest_bazel.main()
