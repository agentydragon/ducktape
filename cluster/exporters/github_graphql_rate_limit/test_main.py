import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from cluster.exporters.github_graphql_rate_limit.main import RateLimit, Settings, create_app, query_rate_limit


def _settings() -> Settings:
    return Settings(account="agentydragon", token="test-token", github_url="https://github.test/graphql")


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


@pytest.mark.parametrize("errors", [False, True])
async def test_query_uses_headers_even_when_graphql_body_reports_exhaustion(errors: bool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(remaining=0, errors=errors))
    ) as client:
        assert await query_rate_limit(client, _settings()) == RateLimit(
            limit=5000, remaining=0, used=5000, reset=1788156036
        )


async def test_query_rejects_rest_rate_limit_headers() -> None:
    response = _response(remaining=5000)
    response.headers["x-ratelimit-resource"] = "core"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(ValueError, match="GraphQL rate-limit resource"):
            await query_rate_limit(client, _settings())


def test_metrics_endpoint_exports_header_values() -> None:
    app = create_app(_settings(), httpx.MockTransport(lambda _: _response(remaining=321)))
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert 'github_graphql_rate_limit{github_account="agentydragon"} 5000' in response.text
    assert 'github_graphql_rate_remaining{github_account="agentydragon"} 321' in response.text
    assert 'github_graphql_rate_used{github_account="agentydragon"} 4679' in response.text
    assert 'github_graphql_rate_reset_timestamp_seconds{github_account="agentydragon"} 1788156036' in response.text


def test_metrics_endpoint_fails_when_headers_are_missing() -> None:
    app = create_app(_settings(), httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {}})))
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 502


if __name__ == "__main__":
    pytest_bazel.main()
