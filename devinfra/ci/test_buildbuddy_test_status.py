"""Tests for publishing live BuildBuddy test status."""

from argparse import Namespace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import pytest_bazel

from devinfra.ci import buildbuddy_test_status


def test_watch_publishes_failure_before_invocation_completes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = Namespace(
        invocation_id="invocation-123",
        sha="abc123",
        state_file=tmp_path / "state.json",
        buildbuddy_url="https://buildbuddy.example",
        github_api_url="https://github.example/api",
        poll_interval=0.01,
    )
    publish_status = Mock()
    monkeypatch.setattr(buildbuddy_test_status, "STOP", Event())
    monkeypatch.setattr(buildbuddy_test_status, "_credentials", lambda: ("bb-key", "gh-token", "owner/repo"))
    monkeypatch.setattr(
        buildbuddy_test_status,
        "_invocation",
        lambda *_args: {"invocationStatus": "PARTIAL_INVOCATION_STATUS", "success": False},
    )
    monkeypatch.setattr(buildbuddy_test_status, "_first_failed_test", lambda *_args: "//pkg:failed_test")
    monkeypatch.setattr(buildbuddy_test_status, "_publish_status", publish_status)

    buildbuddy_test_status._watch(args)

    publish_status.assert_called_once_with(
        repo="owner/repo",
        sha="abc123",
        token="gh-token",
        invocation_id="invocation-123",
        state="failure",
        description="First failing test: //pkg:failed_test",
        state_path=args.state_file,
        github_api_url="https://github.example/api",
        buildbuddy_url="https://buildbuddy.example",
    )


def _finish_args(tmp_path: Path, *, remote_outcome: str, remote_log: Path | None) -> Namespace:
    return Namespace(
        invocation_id="invocation-123",
        sha="abc123",
        state_file=tmp_path / "state.json",
        pid_file=tmp_path / "watcher.pid",
        buildbuddy_url="https://buildbuddy.example",
        github_api_url="https://github.example/api",
        remote_outcome=remote_outcome,
        remote_log=remote_log,
    )


def _unreachable_invocation(*_args: object) -> None:
    raise buildbuddy_test_status.APIError("HTTP Error 500: Internal Server Error")


@pytest.mark.parametrize(
    ("log_text", "remote_outcome", "expected_state"),
    [
        ("affected targets: 0\nNo targets affected by this PR\n", "success", "success"),
        ("affected targets: 3\nCI_BAZEL_COMMAND role=build\n", "success", "success"),
        ("ERROR: bazel-diff failed\n", "failure", "error"),
        (None, "cancelled", "error"),
    ],
)
def test_finish_without_test_invocation_skips_buildbuddy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_text: str | None, remote_outcome: str, expected_state: str
) -> None:
    remote_log = None
    if log_text is not None:
        remote_log = tmp_path / "bb-remote.log"
        remote_log.write_text(log_text)
    publish_status = Mock()
    monkeypatch.setattr(buildbuddy_test_status, "_credentials", lambda: ("bb-key", "gh-token", "owner/repo"))
    monkeypatch.setattr(buildbuddy_test_status, "_invocation", _unreachable_invocation)
    monkeypatch.setattr(buildbuddy_test_status, "_publish_status", publish_status)

    buildbuddy_test_status._finish(_finish_args(tmp_path, remote_outcome=remote_outcome, remote_log=remote_log))

    assert publish_status.call_args.kwargs["state"] == expected_state


def test_finish_reports_started_test_invocation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    remote_log = tmp_path / "bb-remote.log"
    remote_log.write_text("\x1b[90m2026-10-24 17:46:18 UTC \x1b[mCI_BAZEL_COMMAND role=test\n")
    publish_status = Mock()
    monkeypatch.setattr(buildbuddy_test_status, "_credentials", lambda: ("bb-key", "gh-token", "owner/repo"))
    monkeypatch.setattr(
        buildbuddy_test_status,
        "_invocation",
        lambda *_args: {"invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": False},
    )
    monkeypatch.setattr(buildbuddy_test_status, "_publish_status", publish_status)

    buildbuddy_test_status._finish(_finish_args(tmp_path, remote_outcome="failure", remote_log=remote_log))

    assert publish_status.call_args.kwargs["state"] == "failure"


if __name__ == "__main__":
    pytest_bazel.main()
