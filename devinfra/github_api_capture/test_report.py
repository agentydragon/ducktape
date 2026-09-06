import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

import pytest
import pytest_bazel
from mitmproxy import connection, http
from mitmproxy.flow import Error

from devinfra.github_api_capture.report import summarize


@pytest.fixture
def flow() -> http.HTTPFlow:
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("api.github.com", 443)),
    )
    flow.request = http.Request.make(
        "POST",
        "https://api.github.com/graphql?unrelated=test-private-query",
        json.dumps({"query": "query TestQuery { viewer { login } }", "variables": {"secret": "test-private-variable"}}),
        headers=http.Headers(authorization="Bearer test-private-token", user_agent="test-github-client"),
    )
    flow.response = http.Response.make(
        200,
        json.dumps({"data": {"rateLimit": {"cost": 3}, "viewer": {"login": "test-private-response"}}}),
        headers={"content-type": "application/json", "x-ratelimit-resource": "graphql", "x-ratelimit-used": "4000"},
    )
    return flow


def test_report_keeps_request_identity_and_omits_private_payloads(flow: http.HTTPFlow) -> None:
    record = summarize(flow)
    assert record is not None
    assert record.query_sha256 == hashlib.sha256(b"query TestQuery { viewer { login } }").hexdigest()
    assert record.user_agent == "test-github-client"
    assert record.nominal_graphql_cost == 3
    assert record.account_rate_used == 4000
    serialized = json.dumps(asdict(record))
    assert "test-private" not in serialized
    assert "viewer" not in serialized


@pytest.mark.parametrize("payload", [{"data": {}}, {"errors": [{"type": "RATE_LIMIT"}]}])
def test_unknown_cost_never_becomes_account_usage(flow: http.HTTPFlow, payload: dict[str, Any]) -> None:
    assert flow.response is not None
    flow.response.text = json.dumps(payload)
    first = summarize(flow)
    flow.response.headers["x-ratelimit-used"] = "4200"
    second = summarize(flow)
    assert first is not None
    assert second is not None
    assert first.nominal_graphql_cost is None
    assert second.nominal_graphql_cost is None
    assert second.account_rate_used == 4200


def test_transport_failure_retains_request_without_exposing_error(flow: http.HTTPFlow) -> None:
    flow.response = None
    flow.error = Error("test-private-error")
    record = summarize(flow)
    assert record is not None
    assert record.transport_error
    assert record.status is None
    assert record.nominal_graphql_cost is None
    assert "test-private-error" not in json.dumps(asdict(record))


def test_http_200_graphql_error_retains_completion_and_request_id(flow: http.HTTPFlow) -> None:
    assert flow.response is not None
    flow.response.timestamp_end = flow.request.timestamp_start + 12.5
    flow.response.headers["x-github-request-id"] = "ABCD:1234:5678:90AB"
    flow.response.text = json.dumps(
        {"errors": [{"type": "RATE_LIMIT", "code": "graphql_rate_limit", "message": "test-private-error-details"}]}
    )
    record = summarize(flow)
    assert record is not None
    assert record.status == 200
    assert record.completed_at is not None
    assert (
        datetime.fromisoformat(record.completed_at) - datetime.fromisoformat(record.started_at)
    ).total_seconds() == 12.5
    assert record.github_request_id == "ABCD:1234:5678:90AB"
    assert record.graphql_errors is not None
    assert [(error.type, error.code) for error in record.graphql_errors] == [("RATE_LIMIT", "graphql_rate_limit")]
    assert "test-private" not in json.dumps(asdict(record))


def test_non_github_traffic_is_omitted(flow: http.HTTPFlow) -> None:
    flow.request.host = "unrelated.test"
    assert summarize(flow) is None


def test_invalid_headers_are_unknown_without_exposing_values(flow: http.HTTPFlow) -> None:
    assert flow.response is not None
    flow.response.headers["x-github-request-id"] = "test-private-invalid id"
    flow.response.headers["x-ratelimit-used"] = "test-private-invalid int"
    record = summarize(flow)
    assert record is not None
    assert record.github_request_id is None
    assert record.account_rate_used is None
    assert "test-private" not in json.dumps(asdict(record))


if __name__ == "__main__":
    pytest_bazel.main()
