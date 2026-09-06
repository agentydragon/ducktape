import hashlib
import json
from dataclasses import asdict

import pytest
import pytest_bazel
from mitmproxy import connection, http

from devinfra.github_api_capture.cloud_report import summarize


@pytest.fixture
def flow() -> http.HTTPFlow:
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("claude.ai", 443)),
    )
    flow.request = http.Request.make(
        "POST",
        "https://claude.ai/v1/code/github/batch-branch-status?caller=session-sidebar&secret=test-private-query",
        json.dumps(
            {
                "repo_branches": [{"repo": "test-private-repo", "branch": "test-private-branch"}],
                "session_ids": ["test-private-session"],
                "include_ci_status": True,
            }
        ),
        headers=http.Headers(authorization="Bearer test-private-token", cookie="test-private-cookie"),
    )
    flow.response = http.Response.make(200, b"test-private-response")
    return flow


def test_cloud_batch_counts_and_fingerprint_without_payloads(flow: http.HTTPFlow) -> None:
    record = summarize(flow)
    assert record is not None
    assert record.caller == "session-sidebar"
    assert record.repo_branch_count == 1
    assert record.session_count == 1
    assert record.request_sha256 == hashlib.sha256(flow.request.raw_content or b"").hexdigest()
    assert "test-private" not in json.dumps(asdict(record))


def test_absent_batch_arrays_are_unknown_not_zero(flow: http.HTTPFlow) -> None:
    flow.request.text = "{}"
    record = summarize(flow)
    assert record is not None
    assert record.repo_branch_count is None
    assert record.session_count is None
    flow.request.text = '{"repo_branches": [], "session_ids": []}'
    record = summarize(flow)
    assert record is not None
    assert record.repo_branch_count == 0
    assert record.session_count == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/v1/code/github/batch-branch-status",
        "https://claude.ai/v1/code/github/batch-branch-status/other",
        "https://claude.ai/v1/code/sessions/test-private-session",
    ],
)
def test_unrelated_routes_are_omitted(flow: http.HTTPFlow, url: str) -> None:
    flow.request.url = url
    assert summarize(flow) is None


def test_incomplete_cloud_response_is_not_a_success(flow: http.HTTPFlow) -> None:
    flow.response = None
    record = summarize(flow)
    assert record is not None
    assert record.status is None
    assert record.completed_at is None


if __name__ == "__main__":
    pytest_bazel.main()
