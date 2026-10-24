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


if __name__ == "__main__":
    pytest_bazel.main()
