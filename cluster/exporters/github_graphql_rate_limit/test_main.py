import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from cluster.exporters.github_graphql_rate_limit.main import Settings, create_app


def _scrape(upstream: httpx.Response) -> httpx.Response:
    settings = Settings(account="agentydragon", token="test-token", github_url="https://github.test/graphql")

    def github(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == settings.github_url
        assert request.headers["Authorization"] == f"Bearer {settings.token}"
        return upstream

    with TestClient(create_app(settings, httpx.MockTransport(github))) as client:
        return client.get("/metrics")


def _response(*, remaining: int, errors: bool = False) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": str(remaining),
            "x-ratelimit-used": str(5000 - remaining),
            "x-ratelimit-reset": "1788156036",
            "x-ratelimit-resource": "graphql",
        },
        json={"errors": [{"type": "RATE_LIMIT"}]} if errors else {"data": {"rateLimit": {"cost": 1}}},
    )


@pytest.mark.parametrize(("remaining", "errors"), [(321, False), (0, True)])
def test_metrics_endpoint_exports_header_values(remaining: int, errors: bool) -> None:
    response = _scrape(_response(remaining=remaining, errors=errors))

    assert response.status_code == 200
    samples = response.text.splitlines()
    assert 'github_graphql_rate_limit{github_account="agentydragon"} 5000' in samples
    assert f'github_graphql_rate_remaining{{github_account="agentydragon"}} {remaining}' in samples
    assert f'github_graphql_rate_used{{github_account="agentydragon"}} {5000 - remaining}' in samples
    assert 'github_graphql_rate_reset_timestamp_seconds{github_account="agentydragon"} 1788156036' in samples


def test_metrics_endpoint_rejects_rest_bucket() -> None:
    upstream = _response(remaining=321)
    upstream.headers["x-ratelimit-resource"] = "core"
    assert _scrape(upstream).status_code == 502


def test_metrics_endpoint_rejects_missing_quota_header() -> None:
    upstream = _response(remaining=321)
    del upstream.headers["x-ratelimit-remaining"]
    assert _scrape(upstream).status_code == 502


if __name__ == "__main__":
    pytest_bazel.main()
