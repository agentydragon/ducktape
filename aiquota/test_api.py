import asyncio
import base64
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import pytest_bazel

from aiquota import api
from aiquota.api import (
    BackgroundCollector,
    BrowserOAuth,
    CollectorMetrics,
    QuotaAPIService,
    SnapshotFeed,
    _CapturingClientFactory,
    create_app,
)
from aiquota.config import Config
from aiquota.models import (
    AllQuotas,
    FetchError,
    FetchSuccess,
    ProviderFetch,
    ProviderQuota,
    QuotaSnapshot,
    QuotaWindow,
    RawUpstreamResponse,
)
from aiquota.render.view_model import AllQuotasView
from aiquota.settings import Settings
from util.testing.asgi import serve_app_in_loop

if __name__ == "__main__":
    pytest_bazel.main()


class FakeFetcher:
    def __init__(self) -> None:
        now = datetime.now(UTC)
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
                ),
                "claude_prepaid_balance": RawUpstreamResponse(
                    status_code=200,
                    content_type="application/json",
                    body={"balance": {"credits": {"amount_minor": 12345, "exponent": 2}}, "currency": "USD"},
                ),
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
        response = await browser.get("/v1/events")
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


@pytest.mark.parametrize("path", ["/v1/quotas", "/v1/events"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-bearer"}])
async def test_quota_endpoints_require_bearer(client: httpx.AsyncClient, path: str, headers: dict[str, str]) -> None:
    response = await client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_quotas_serve_the_view_model_without_the_raw_payload(client: httpx.AsyncClient) -> None:
    """The browser and the console both need the derived policy bits, not just the raw windows.

    Re-deriving `currently_over_plan` per surface is what drifted before (aiquota/AGENTS.md),
    so the endpoint serves the shared view model rather than the bare snapshot.
    """
    response = await client.get("/v1/quotas", headers={"Authorization": "Bearer test-bearer"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    provider = response.json()["providers"][0]
    assert provider["last_output"]["result"]["windows"][0]["used_percent"] == 45.0
    assert provider["currently_over_plan"] is False
    assert provider["extra_status"] == "none"
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


async def test_prepaid_balance_raw_capture_does_not_replace_claude_usage_raw(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Bearer test-bearer"}
    usage_response = await client.get("/v1/providers/claude/raw", headers=headers)
    prepaid_response = await client.get("/v1/providers/claude/prepaid-balance/raw", headers=headers)

    assert usage_response.status_code == 200
    assert usage_response.json()["upstream"]["body"]["five_hour"]["utilization"] == 45.0
    assert prepaid_response.status_code == 200
    assert prepaid_response.json()["upstream"]["body"]["balance"]["credits"] == {"amount_minor": 12345, "exponent": 2}


async def test_real_claude_provider_keeps_usage_and_prepaid_captures_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    org_uuid = "123e4567-e89b-12d3-a456-426614174000"
    balance_url = f"https://api.anthropic.com/api/oauth/organizations/{org_uuid}/prepaid/credits"
    usage_body = {"five_hour": {"utilization": 10}}
    profile_body = {"organization": {"uuid": org_uuid}}
    balance_body = {
        "currency": "USD",
        "balance": {"credits": {"amount_minor": 12345, "exponent": 2}},
        "balance_credits": 123,
        "tranches": [{"remaining_amount_minor_units": 12000}],
    }

    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        bodies = {
            "https://api.anthropic.com/api/oauth/usage": usage_body,
            "https://api.anthropic.com/api/oauth/profile": profile_body,
            balance_url: balance_body,
        }
        return httpx.Response(200, json=bodies[str(request.url)], request=request)

    original_factory = api._CapturingClientFactory

    def factory_with_mock_transport(
        *, claude_proxy: str | None, claude_proxy_ca: Path | None
    ) -> _CapturingClientFactory:
        return original_factory(
            claude_proxy=claude_proxy, claude_proxy_ca=claude_proxy_ca, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(api, "_CapturingClientFactory", factory_with_mock_transport)
    service = QuotaAPIService(
        Config.model_validate(
            {"claude": {"access_token": "placeholder"}, "codex": {"enabled": False}, "zai": {"enabled": False}}
        )
    )

    snapshot = await service.fetch(force_refresh=True)

    assert set(requested_urls) == {
        "https://api.anthropic.com/api/oauth/usage",
        "https://api.anthropic.com/api/oauth/profile",
        balance_url,
    }
    assert set(snapshot.raw_responses) == {"claude", "claude_prepaid_balance"}
    assert snapshot.raw_responses["claude"].body == usage_body
    assert snapshot.raw_responses["claude_prepaid_balance"].body == balance_body
    output = snapshot.quotas.providers[0].last_output.result
    assert isinstance(output, FetchSuccess)
    assert output.prepaid_balance is not None
    assert output.prepaid_balance.model_dump() == {
        "kind": "available",
        "amount_minor": 12345,
        "exponent": 2,
        "currency": "USD",
    }


async def test_prepaid_balance_raw_capture_is_claude_only(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/v1/providers/codex/prepaid-balance/raw", headers={"Authorization": "Bearer test-bearer"}
    )

    assert response.status_code == 404


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


def test_legacy_claude_proxy_routes_only_direct_provider_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[dict[str, object]] = []

    class RecordingAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            clients.append(kwargs)

    monkeypatch.setattr(api.httpx, "AsyncClient", RecordingAsyncClient)
    factory = _CapturingClientFactory(claude_proxy="http://credential-proxy.test:8180", claude_proxy_ca=None)

    factory("claude", {"http://cliproxy.test/api-call"}, 5.0)
    factory("claude_prepaid_balance", {"http://cliproxy.test/api-call"}, 5.0)
    factory("claude", {"https://api.anthropic.com/api/oauth/usage"}, 5.0)
    factory("claude_profile_direct", set(), 5.0)
    factory("claude_prepaid_balance", {"https://api.anthropic.com/api/oauth/organizations/test/prepaid/credits"}, 5.0)
    service_with_both_configs = QuotaAPIService(
        Config.model_validate({"cli_proxy_api": {"url": "http://cliproxy.test/v0/management"}}),
        claude_proxy="http://credential-proxy.test:8180",
    )
    management_factory = service_with_both_configs._client_factory()
    management_factory("claude", {"http://cliproxy.test/v0/management/api-call"}, 5.0)
    management_factory("claude_prepaid_balance", {"http://cliproxy.test/v0/management/api-call"}, 5.0)

    assert "proxy" not in clients[0]
    assert "proxy" not in clients[1]
    assert clients[2]["proxy"] == "http://credential-proxy.test:8180"
    assert clients[3]["proxy"] == "http://credential-proxy.test:8180"
    assert clients[4]["proxy"] == "http://credential-proxy.test:8180"
    assert "proxy" not in clients[5]
    assert "proxy" not in clients[6]


def _make_snapshot(used_percent: float, fetched_at: datetime) -> QuotaSnapshot:
    return QuotaSnapshot(
        quotas=AllQuotas(
            fetched_at=fetched_at,
            providers=[
                ProviderQuota(
                    provider="claude",
                    last_output=ProviderFetch(
                        fetched_at=fetched_at,
                        result=FetchSuccess(
                            windows=[
                                QuotaWindow(used_percent=used_percent, reset_seconds=3600, window_seconds=5 * 3600)
                            ]
                        ),
                    ),
                )
            ],
        ),
        raw_responses={},
    )


async def _read_view(lines: AsyncIterator[str]) -> AllQuotasView:
    event: str | None = None
    data: str | None = None
    async for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = line.removeprefix("data: ")
        elif not line and event is not None:
            assert event == "view"
            assert data is not None
            return AllQuotasView.model_validate_json(data)
    raise AssertionError("event stream ended before a view")


async def test_events_send_current_view_and_broadcast_on_demand_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    class CountingProvider:
        name = "claude"

        def __init__(self) -> None:
            self.fetches = 0
            self.used_percent = 45.0
            self.fail = False

        async def fetch(self) -> ProviderFetch:
            self.fetches += 1
            if self.fail:
                return ProviderFetch(fetched_at=datetime.now(UTC), result=FetchError(error="provider unavailable"))
            return ProviderFetch(
                fetched_at=datetime.now(UTC),
                result=FetchSuccess(
                    windows=[QuotaWindow(used_percent=self.used_percent, reset_seconds=3600, window_seconds=5 * 3600)]
                ),
            )

    provider = CountingProvider()
    monkeypatch.setattr(api, "_instantiate", lambda _config, **_kwargs: [provider])
    service = QuotaAPIService(Config())
    app = create_app(bearer_token="test-bearer", fetcher=service)
    feed: SnapshotFeed = app.state.snapshot_feed
    auth = {"Authorization": "Bearer test-bearer"}

    async with (
        serve_app_in_loop(app) as url,
        httpx.AsyncClient(base_url=url) as client,
        client.stream("GET", "/v1/events", headers=auth) as first,
        client.stream("GET", "/v1/events", headers=auth) as second,
    ):
        first_lines = first.aiter_lines().__aiter__()
        initial = await _read_view(first_lines)
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["x-accel-buffering"] == "no"
        initial_result = initial.providers[0].last_output.result
        assert isinstance(initial_result, FetchSuccess)
        assert initial_result.windows[0].used_percent == 45.0
        quotas_response = await client.get("/v1/quotas", headers=auth)
        assert quotas_response.status_code == 200
        assert initial.model_dump(mode="json") == quotas_response.json()

        second_lines = second.aiter_lines().__aiter__()
        second_view = await _read_view(second_lines)
        assert second_view == initial
        assert provider.fetches == 1  # The second client used the service's fresh cache.

        service._cache_ttl = timedelta(0)
        provider.used_percent = 52.0
        quota_response = await client.get("/v1/quotas", headers=auth)
        assert quota_response.status_code == 200
        first_update = await _read_view(first_lines)
        second_update = await _read_view(second_lines)
        first_update_result = first_update.providers[0].last_output.result
        assert isinstance(first_update_result, FetchSuccess)
        assert first_update_result.windows[0].used_percent == 52.0
        assert second_update == first_update
        assert provider.fetches == 2  # One refresh reached both clients.

        provider.fail = True
        stale_response = await client.get("/v1/quotas", headers=auth)
        assert stale_response.status_code == 200
        stale_update = await _read_view(first_lines)
        assert stale_update.model_dump(mode="json") == stale_response.json()
        stale_provider = stale_update.providers[0]
        assert isinstance(stale_provider.last_output.result, FetchError)
        assert stale_provider.last_success is not None
        assert stale_provider.last_success.result.windows[0].used_percent == 52.0
        assert provider.fetches == 3

    await feed.wait_for_subscribers(0)


@pytest.mark.parametrize("start_with_collector", [False, True])
async def test_initial_sse_and_collector_fetch_are_coalesced(start_with_collector: bool) -> None:
    class BlockingFetcher(FakeFetcher):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return self.snapshot

    fetcher = BlockingFetcher()

    class TrackedFeed(SnapshotFeed):
        def __init__(self, source: BlockingFetcher) -> None:
            super().__init__(source)
            self.requests = 0
            self.second_request = asyncio.Event()

        async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
            self.requests += 1
            if self.requests == 2:
                self.second_request.set()
            return await super().fetch(force_refresh=force_refresh)

    feed = TrackedFeed(fetcher)
    collector = BackgroundCollector(feed, None, interval=timedelta(seconds=300), metrics=CollectorMetrics())

    if start_with_collector:
        collection = asyncio.create_task(collector.poll_once())
        await fetcher.started.wait()
        subscription_task = asyncio.create_task(feed.open_subscription())
    else:
        subscription_task = asyncio.create_task(feed.open_subscription())
        await fetcher.started.wait()
        collection = asyncio.create_task(collector.poll_once())
    await feed.second_request.wait()
    fetcher.release.set()
    subscription = await subscription_task
    await collection

    assert fetcher.calls == 1
    assert feed.latest is subscription.snapshot
    await feed.close_subscription(subscription.token)


async def test_background_collection_runs_without_clickhouse() -> None:
    fetcher = FakeFetcher()
    feed = SnapshotFeed(fetcher)
    collector = BackgroundCollector(feed, None, interval=timedelta(seconds=300), metrics=CollectorMetrics())
    app = create_app(bearer_token="test-bearer", fetcher=feed, collector=collector)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aiquota.test") as client:
        assert (await client.get("/readyz")).status_code == 503
        async with app.router.lifespan_context(app):
            await feed.wait_after(0)
            assert collector.is_running
            assert feed.latest is not None
            assert (await client.get("/readyz")).status_code == 200
    assert not collector.is_running


async def test_clickhouse_failure_does_not_block_snapshot_publication() -> None:
    class FailingSink:
        async def write(self, snapshot: QuotaSnapshot) -> int:
            raise OSError("ClickHouse unavailable")

    feed = SnapshotFeed(FakeFetcher())
    collector = BackgroundCollector(feed, FailingSink(), interval=timedelta(seconds=300), metrics=CollectorMetrics())
    app = create_app(bearer_token="test-bearer", fetcher=feed, collector=collector)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://aiquota.test") as client,
        app.router.lifespan_context(app),
    ):
        await feed.wait_after(0)
        assert feed.latest is not None
        assert not collector.has_persisted
        assert (await client.get("/readyz")).status_code == 503


async def test_storage_is_prepared_before_collection_starts() -> None:
    events: list[str] = []

    class RecordingFetcher(FakeFetcher):
        async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
            events.append("fetch")
            return await super().fetch(force_refresh)

    async def prepare_storage() -> None:
        events.append("prepare")

    feed = SnapshotFeed(RecordingFetcher())
    collector = BackgroundCollector(feed, None, interval=timedelta(seconds=300), metrics=CollectorMetrics())
    app = create_app(bearer_token="test-bearer", fetcher=feed, collector=collector, prepare_storage=prepare_storage)
    async with app.router.lifespan_context(app):
        await feed.wait_after(0)

    assert events[:2] == ["prepare", "fetch"]


async def test_failed_storage_preparation_aborts_startup_before_collection() -> None:
    fetcher = FakeFetcher()
    feed = SnapshotFeed(fetcher)
    collector = BackgroundCollector(feed, None, interval=timedelta(seconds=300), metrics=CollectorMetrics())

    async def prepare_storage() -> None:
        raise OSError("ClickHouse unavailable")

    app = create_app(bearer_token="test-bearer", fetcher=feed, collector=collector, prepare_storage=prepare_storage)

    with pytest.raises(OSError, match="ClickHouse unavailable"):
        async with app.router.lifespan_context(app):
            raise AssertionError("startup must not complete")

    assert fetcher.calls == 0
    assert not collector.is_running


async def test_peak_transition_republishes_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = FakeFetcher()
    feed = SnapshotFeed(fetcher)
    subscription = await feed.open_subscription()
    monkeypatch.setattr(api, "_next_view_transition", lambda _snapshot: datetime.now(UTC) + timedelta(seconds=0.02))
    transition_task = asyncio.create_task(feed.run_time_transitions())
    try:
        revision, snapshot = await asyncio.wait_for(feed.wait_after(subscription.revision), timeout=0.5)
        assert revision > subscription.revision
        assert snapshot is subscription.snapshot
        assert fetcher.calls == 1
    finally:
        transition_task.cancel()
        await asyncio.gather(transition_task, return_exceptions=True)
        await feed.close_subscription(subscription.token)


async def test_old_peak_timer_cannot_replace_a_newer_snapshot() -> None:
    fetcher = FakeFetcher()
    feed = SnapshotFeed(fetcher)
    subscription = await feed.open_subscription()
    newer = _make_snapshot(60.0, datetime.now(UTC) + timedelta(seconds=1))
    newer_revision = await feed.publish(newer)

    await feed.publish_time_transition(subscription.snapshot, subscription.revision)

    assert feed.latest is newer
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(feed.wait_after(newer_revision), timeout=0.01)
    await feed.close_subscription(subscription.token)


async def test_event_stream_heartbeats_and_disconnect_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_SSE_HEARTBEAT_SECONDS", 0.01)
    fetcher = FakeFetcher()
    app = create_app(bearer_token="test-bearer", fetcher=fetcher)
    feed: SnapshotFeed = app.state.snapshot_feed
    async with (
        serve_app_in_loop(app) as url,
        httpx.AsyncClient(base_url=url) as client,
        client.stream("GET", "/v1/events", headers={"Authorization": "Bearer test-bearer"}) as response,
    ):
        lines = response.aiter_lines().__aiter__()
        assert (await _read_view(lines)).providers
        assert await anext(lines) == ": heartbeat"
        assert await anext(lines) == ""
        assert fetcher.calls == 1  # Heartbeats do not poll providers per client.
    async with asyncio.timeout(1):
        await feed.wait_for_subscribers(0)
