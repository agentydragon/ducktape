import json

import pytest
import pytest_bazel
from aiohttp.test_utils import TestClient, TestServer
from mitmproxy import connection, http

from cluster.proxies.github_api_proxy.metrics import (
    CLIENT_METADATA_KEY,
    MAX_COST_BODY_BYTES,
    CostStatus,
    Metrics,
    observed_cost,
)


@pytest.mark.parametrize(
    ("payload", "status", "cost"),
    [
        ({"data": {"rateLimit": {"cost": 7}}}, CostStatus.OBSERVED, 7),
        ({"data": {"rateLimit": {"cost": 0}}}, CostStatus.OBSERVED, 0),
        ({"data": {"rateLimit": {"cost": True}}}, CostStatus.INVALID, None),
        ({"data": {"rateLimit": {"cost": -1}}}, CostStatus.INVALID, None),
        ({"data": {"rateLimit": {"cost": "test-private"}}}, CostStatus.INVALID, None),
        ({"data": {}}, CostStatus.ABSENT, None),
        ({"errors": [{"message": "test-private"}]}, CostStatus.ABSENT, None),
    ],
)
def test_explicit_cost_only(payload: dict, status: CostStatus, cost: int | None) -> None:
    response = http.Response.make(200, json.dumps(payload), http.Headers(x_ratelimit_used="999"))
    assert observed_cost(response) == (status, cost)


def test_unavailable_invalid_and_bounded_cost() -> None:
    assert observed_cost(None) == (CostStatus.UNAVAILABLE, None)
    assert observed_cost(http.Response.make(200, b"test-private-non-json")) == (CostStatus.INVALID, None)
    assert observed_cost(http.Response.make(200, b"x" * (MAX_COST_BODY_BYTES + 1))) == (CostStatus.OVERSIZED, None)
    compressed = http.Response.make(200, b"test-private-compressed", http.Headers(content_encoding="gzip"))
    assert observed_cost(compressed) == (CostStatus.UNAVAILABLE, None)


async def test_private_health_and_bounded_metrics() -> None:
    metrics = Metrics()
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("api.github.com", 443)),
    )
    flow.request = http.Request.make("POST", "https://api.github.com/graphql?test-private=token", b"test-private-query")
    flow.metadata[CLIENT_METADATA_KEY] = "test-alpha"
    for used, payload in [(100, {"data": {"rateLimit": {"cost": 2}}}), (1000, {"data": {}})]:
        flow.response = http.Response.make(200, json.dumps(payload), http.Headers(x_ratelimit_used=str(used)))
        metrics.response(flow)
    assert (
        metrics.registry.get_sample_value("github_api_proxy_graphql_observed_cost_total", {"client": "test-alpha"}) == 2
    )
    async with TestClient(TestServer(metrics.application())) as client:
        assert (await client.get("/healthz")).status == 503
        metrics.running()
        assert (await client.get("/healthz")).status == 200
        response = await client.get("/metrics")
        assert response.status == 200
        exposition = await response.text()
        assert 'client="test-alpha",result="absent"' in exposition
        assert "test-private" not in exposition
        assert "x-ratelimit-used" not in exposition
        assert "api.github.com" not in exposition


if __name__ == "__main__":
    pytest_bazel.main()
