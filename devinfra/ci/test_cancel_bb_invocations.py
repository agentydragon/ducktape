"""Cancellation is scoped to the child IDs supplied by this workflow run."""

import json
import sys
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from uuid import uuid4

import pytest
import pytest_bazel

from devinfra.ci import cancel_bb_invocations


def test_requests_only_supplied_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["cancel", ",".join(ids)])
    with patch.object(cancel_bb_invocations, "urlopen", MagicMock()) as urlopen, pytest.raises(SystemExit) as exit_info:
        cancel_bb_invocations.main()
    assert exit_info.value.code == 0
    assert len(urlopen.call_args_list) == 2
    requests = [call.args[0] for call in urlopen.call_args_list]
    assert {json.loads(request.data)["invocationId"] for request in requests} == set(ids)
    for call, request in zip(urlopen.call_args_list, requests, strict=True):
        assert request.full_url == "https://app.buildbuddy.io/rpc/BuildBuddyService/CancelExecutions"
        assert request.get_header("X-buildbuddy-api-key") == "test-key"
        assert call.kwargs == {"timeout": 4}


@pytest.mark.parametrize("error", [URLError("unavailable"), TimeoutError()])
def test_request_failure_is_visible(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
    with patch.object(cancel_bb_invocations, "urlopen", side_effect=error):
        assert not cancel_bb_invocations.cancel(str(uuid4()))


def test_no_known_children_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUILDBUDDY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["cancel", ""])
    cancel_bb_invocations.main()


if __name__ == "__main__":
    pytest_bazel.main()
