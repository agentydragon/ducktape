import base64
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import pytest_bazel

from aiquota.api import BrowserOAuth, QuotaSnapshot, RawUpstreamResponse, Settings, _CapturingClientFactory, create_app
from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow

if __name__ == "__main__":
    pytest_bazel.main()

pytestmark = pytest.mark.asyncio


class FakeFetcher:
    def __init__(self) -> None:
        now = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
        self.calls = 0
        self.snapshot = QuotaSnapshot(
            quotas=AllQuotas(
                fetched_at=now,
                providers=[
                    ProviderQuota(
                        provider="claude",
                        last_output=ProviderFetch(
                            fetched_at=now,
                            result=FetchSuccess(
                                windows=[QuotaWindow(used_percent=45.0, reset_seconds=3600, window_seconds=5 * 3600)]
                            ),
                        ),
                    )
                ],
            ),
            raw_responses={
                "claude": RawUpstreamResponse(
                    status_code=200,
                    content_type="application/json",
                    body={"five_hour": {"utilization": 45.0, "internal_detail": "upstream-only"}},
                )
            },
        )

    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
        self.calls += 1
        return self.snapshot


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(bearer_token="test-bearer", fetcher=FakeFetcher())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as result:
        yield result


def test_settings_are_loaded_and_validated_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIQUOTA_API_BEARER_TOKEN", "test-bearer")
    monkeypatch.setenv("AIQUOTA_CONFIG", "/tmp/aiquota.toml")
    monkeypatch.setenv("AIQUOTA_CACHE_TTL_SECONDS", "30")
    monkeypatch.setenv("AIQUOTA_CLAUDE_PROXY", "http://proxy:8180")
    monkeypatch.setenv("AIQUOTA_CLAUDE_PROXY_CA", "/tmp/proxy-ca.pem")
    monkeypatch.setenv("AIQUOTA_CLIPROXY_API_KEY", "management-key")
    monkeypatch.setenv("AIQUOTA_CLICKHOUSE_URL", "http://clickhouse:8123")
    monkeypatch.setenv("AIQUOTA_CLICKHOUSE_PASSWORD", "clickhouse-password")
    monkeypatch.setenv("AIQUOTA_POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("AIQUOTA_OAUTH_ISSUER", "https://auth.test/application/o/aiquota/")
    monkeypatch.setenv("AIQUOTA_OAUTH_CLIENT_ID", "aiquota")
    monkeypatch.setenv("AIQUOTA_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("AIQUOTA_OAUTH_SESSION_SECRET", "session-secret")

    settings = Settings()

    assert settings.api_bearer_token == "test-bearer"
    assert settings.config_path == Path("/tmp/aiquota.toml")
    assert settings.cache_ttl.total_seconds() == 30
    assert settings.claude_proxy == "http://proxy:8180"
    assert settings.claude_proxy_ca == Path("/tmp/proxy-ca.pem")
    assert settings.cli_proxy_api_key is not None
    assert settings.cli_proxy_api_key.get_secret_value() == "management-key"
    assert settings.clickhouse_url == "http://clickhouse:8123"
    assert settings.clickhouse_password is not None
    assert settings.clickhouse_password.get_secret_value() == "clickhouse-password"
    assert settings.poll_interval_seconds == 300


async def test_quota_api_requires_an_oauth_session_or_bearer() -> None:
    app = create_app(
        bearer_token="test-bearer",
        fetcher=FakeFetcher(),
        browser_oauth=BrowserOAuth(
            issuer="https://auth.test/application/o/aiquota/",
            client_id="aiquota",
            client_secret="client-secret",
            session_secret="session-secret",
            public_base_url="https://aiquota.test",
            username="agentydragon",
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://aiquota.test") as browser:
        response = await browser.get("/v1/quotas")
    assert response.status_code == 401


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_is_public_when_background_collection_is_disabled(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_metrics_are_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "aiquota_collector_ready" in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-bearer"}])
async def test_quota_endpoints_require_bearer(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await client.get("/v1/quotas", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_normalized_quotas_include_remaining_but_not_raw_payload(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/quotas", headers={"Authorization": "Bearer test-bearer"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    window = body["providers"][0]["last_output"]["result"]["windows"][0]
    assert window["used_percent"] == 45.0
    assert window["remaining_percent"] == 55.0
    assert "internal_detail" not in response.text


async def test_raw_provider_response_is_available_to_authenticated_callers(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/providers/claude/raw", headers={"Authorization": "Bearer test-bearer"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["upstream"] == {
        "status_code": 200,
        "content_type": "application/json",
        "body": {"five_hour": {"utilization": 45.0, "internal_detail": "upstream-only"}},
        "body_base64": None,
        "body_sha256": None,
        "body_size_bytes": None,
        "truncated": False,
    }


async def test_unknown_provider_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/providers/codex/raw", headers={"Authorization": "Bearer test-bearer"})
    assert response.status_code == 404


async def test_capture_preserves_exact_bounded_upstream_bytes() -> None:
    body = b'{"value": 1, "spacing": "preserved"}\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"}, request=request)

    factory = _CapturingClientFactory(claude_proxy=None, claude_proxy_ca=None, transport=httpx.MockTransport(handler))
    url = "https://provider.example/usage"
    async with factory("test", {url}, 5.0) as upstream:
        response = await upstream.get(url)
        response.raise_for_status()

    raw = factory.responses["test"]
    assert raw.body == {"value": 1, "spacing": "preserved"}
    assert raw.body_base64 is not None
    assert base64.b64decode(raw.body_base64) == body
    assert raw.body_size_bytes == len(body)
    assert raw.truncated is False
