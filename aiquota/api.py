"""Bearer-authenticated HTTP API for the aiquota provider adapters.

The API deliberately keeps provider responses in process memory only.  It is
safe to use beside a credential-refresh owner (for example CLIProxyAPI): its
provider settings disable refreshes, so this process never mutates credential
files or persists quota snapshots.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, cast

import anyio
import httpx
import uvicorn
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from starlette.middleware.sessions import SessionMiddleware

from aiquota.cache import _assemble, _instantiate
from aiquota.clickhouse import ClickHouseSnapshotSink
from aiquota.config import Config, load as load_config
from aiquota.models import AllQuotas, FetchSuccess, HistorySnapshot, QuotaSnapshot, RawUpstreamResponse
from aiquota.providers.base import SupportsHistory
from aiquota.render.view_model import AllQuotasView, to_view
from aiquota.settings import Settings
from util.bazel.runfiles import find_path

_CACHE_CONTROL = {"Cache-Control": "no-store"}
_MAX_CAPTURE_BYTES = 1024 * 1024
_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

_FRONTEND_INDEX = "_main/aiquota/frontend/dist/index.html"
_SSE_HEARTBEAT_SECONDS = 10


class SnapshotFetcher(Protocol):
    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot: ...


class SnapshotSink(Protocol):
    async def write(self, snapshot: QuotaSnapshot) -> int: ...


class HistoryFetcher(Protocol):
    async def fetch_history(self) -> HistorySnapshot: ...


class HistorySink(Protocol):
    async def write_history(self, snapshot: HistorySnapshot) -> int: ...


@dataclass(frozen=True)
class SnapshotSubscription:
    token: int
    revision: int
    snapshot: QuotaSnapshot


class SnapshotFeed:
    """One shared latest-snapshot feed for HTTP reads and event-stream clients.

    Updates are kept as a single current value. Subscribers wait on a revision
    predicate, so a slow client skips intermediate snapshots instead of
    accumulating an unbounded queue.
    """

    def __init__(self, fetcher: SnapshotFetcher) -> None:
        self._fetcher = fetcher
        self._condition = asyncio.Condition()
        self._fetch_lock = asyncio.Lock()
        self._snapshot: QuotaSnapshot | None = None
        self._revision = 0
        self._fetch_revision = 0
        self._next_subscriber = 0
        self._subscribers: set[int] = set()

    @property
    def latest(self) -> QuotaSnapshot | None:
        return self._snapshot

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
        # Calls that were already waiting when a refresh began share its result,
        # including forced collector calls. A later forced call starts a new
        # provider poll as requested.
        observed_fetch_revision = self._fetch_revision
        async with self._fetch_lock:
            if self._fetch_revision != observed_fetch_revision and self._snapshot is not None:
                return self._snapshot
            snapshot = await self._fetcher.fetch(force_refresh=force_refresh)
            self._fetch_revision += 1
            await self.publish(snapshot)
            return snapshot

    async def ensure_current(self) -> QuotaSnapshot:
        # Let the source's existing 120-second cache decide whether reconnects
        # need a provider refresh; the feed still coalesces concurrent misses.
        return await self.fetch()

    async def publish(self, snapshot: QuotaSnapshot) -> int:
        async with self._condition:
            if self._snapshot == snapshot:
                return self._revision
            self._snapshot = snapshot
            self._revision += 1
            self._condition.notify_all()
            return self._revision

    async def publish_time_transition(self, snapshot: QuotaSnapshot, expected_revision: int) -> None:
        async with self._condition:
            if self._revision != expected_revision or self._snapshot is not snapshot:
                return
            self._revision += 1
            self._condition.notify_all()

    async def subscribe_current(self) -> SnapshotSubscription:
        async with self._condition:
            assert self._snapshot is not None
            self._next_subscriber += 1
            token = self._next_subscriber
            self._subscribers.add(token)
            self._condition.notify_all()
            return SnapshotSubscription(token, self._revision, self._snapshot)

    async def open_subscription(self) -> SnapshotSubscription:
        await self.ensure_current()
        return await self.subscribe_current()

    async def close_subscription(self, token: int) -> None:
        async with self._condition:
            self._subscribers.discard(token)
            self._condition.notify_all()

    async def wait_for_subscribers(self, count: int) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: len(self._subscribers) == count)

    async def wait_after(self, revision: int) -> tuple[int, QuotaSnapshot]:
        async with self._condition:
            await self._condition.wait_for(lambda: self._revision > revision)
            assert self._snapshot is not None
            return self._revision, self._snapshot

    async def _wait_for_snapshot(self) -> tuple[int, QuotaSnapshot]:
        async with self._condition:
            await self._condition.wait_for(lambda: self._snapshot is not None)
            assert self._snapshot is not None
            return self._revision, self._snapshot

    async def run_time_transitions(self) -> None:
        """Republish when a view's time-derived peak-burn status changes."""
        while True:
            revision, snapshot = await self._wait_for_snapshot()
            transition = _next_view_transition(snapshot)
            if transition is None:
                await self.wait_after(revision)
                continue
            delay = max(0.0, (transition - datetime.now(UTC)).total_seconds())
            try:
                await asyncio.wait_for(self.wait_after(revision), timeout=delay)
            except TimeoutError:
                # A new revision is useful even though the provider snapshot is
                # unchanged: clients must recompute its time-derived view now.
                # The revision guard prevents a timer from replacing a newer
                # provider snapshot that arrived as its timeout expired.
                await self.publish_time_transition(snapshot, revision)


def _next_view_transition(snapshot: QuotaSnapshot) -> datetime | None:
    changes = [provider.burn.changes_at for provider in to_view(snapshot.quotas).providers if provider.burn is not None]
    return min(changes) if changes else None


def _view_event(snapshot: QuotaSnapshot) -> bytes:
    return f"event: view\ndata: {to_view(snapshot.quotas).model_dump_json()}\n\n".encode()


class CollectorMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.polls = Counter(
            "aiquota_poll_total", "Background quota collection attempts", ["result"], registry=self.registry
        )
        self.provider_success = Gauge(
            "aiquota_provider_scrape_success",
            "Whether the latest provider fetch succeeded",
            ["provider"],
            registry=self.registry,
        )
        self.last_success = Gauge(
            "aiquota_last_persisted_timestamp_seconds",
            "Unix timestamp of the latest snapshot persisted to ClickHouse",
            registry=self.registry,
        )
        self.clickhouse_writes = Counter(
            "aiquota_clickhouse_write_total", "ClickHouse batch writes", ["result"], registry=self.registry
        )
        self.clickhouse_rows = Counter(
            "aiquota_clickhouse_rows_total", "Rows appended to ClickHouse", registry=self.registry
        )
        self.history_polls = Counter(
            "aiquota_history_poll_total", "Provider history collection attempts", ["result"], registry=self.registry
        )
        self.history_rows = Counter(
            "aiquota_history_rows_total", "History observations appended to ClickHouse", registry=self.registry
        )
        self.ready = Gauge(
            "aiquota_collector_ready",
            "Whether at least one snapshot has been persisted to ClickHouse",
            registry=self.registry,
        )


class BackgroundCollector:
    """Continuously refresh providers and optionally append snapshots to a sink."""

    def __init__(
        self, fetcher: SnapshotFetcher, sink: SnapshotSink | None, *, interval: timedelta, metrics: CollectorMetrics
    ) -> None:
        self._fetcher = fetcher
        self._sink = sink
        self._interval = interval
        self.metrics = metrics
        self.has_persisted = False
        self.is_running = False
        self._has_collected = False

    @property
    def requires_persistence_for_readiness(self) -> bool:
        return self._sink is not None

    def bind_fetcher(self, fetcher: SnapshotFetcher) -> None:
        """Use the app's publication feed for collector snapshots as well."""
        self._fetcher = fetcher

    async def run(self) -> None:
        self.is_running = True
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self._interval.total_seconds())
        finally:
            self.is_running = False

    async def poll_once(self) -> None:
        try:
            # The first poll joins a simultaneous lazy GET/SSE seed fetch. Later
            # polls preserve the configured unconditional refresh cadence.
            snapshot = await self._fetcher.fetch(force_refresh=self._has_collected)
            self._has_collected = True
        except httpx.HTTPError, OSError:
            self.metrics.polls.labels(result="error").inc()
            logger.exception("aiquota quota fetch failed")
            return
        for provider in snapshot.quotas.providers:
            self.metrics.provider_success.labels(provider=provider.provider).set(
                1 if isinstance(provider.last_output.result, FetchSuccess) else 0
            )
        if self._sink is None:
            self.metrics.polls.labels(result="success").inc()
            return
        try:
            rows = await self._sink.write(snapshot)
        except httpx.HTTPError, OSError:
            self.metrics.polls.labels(result="error").inc()
            self.metrics.clickhouse_writes.labels(result="error").inc()
            logger.exception("aiquota ClickHouse write failed")
            return
        self.metrics.polls.labels(result="success").inc()
        self.metrics.clickhouse_writes.labels(result="success").inc()
        self.metrics.clickhouse_rows.inc(rows)
        self.metrics.last_success.set(datetime.now(UTC).timestamp())
        self.metrics.ready.set(1)
        self.has_persisted = True


class HistoryCollector:
    """Poll the provider history endpoints on their own, slower schedule.

    Kept apart from the quota collector because these endpoints restate months
    of unchanged history on every call: polling them at the quota cadence would
    rewrite a whole year every five minutes for no new information.
    """

    def __init__(
        self, fetcher: HistoryFetcher, sink: HistorySink, *, interval: timedelta, metrics: CollectorMetrics
    ) -> None:
        self._fetcher = fetcher
        self._sink = sink
        self._interval = interval
        self.metrics = metrics
        self.is_running = False

    async def run(self) -> None:
        self.is_running = True
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self._interval.total_seconds())
        finally:
            self.is_running = False

    async def poll_once(self) -> None:
        try:
            rows = await self._sink.write_history(await self._fetcher.fetch_history())
        except httpx.HTTPError, OSError:
            self.metrics.history_polls.labels(result="error").inc()
            logger.exception("aiquota history collection failed")
            return
        self.metrics.history_polls.labels(result="success").inc()
        self.metrics.history_rows.inc(rows)


class _CapturingClientFactory:
    """Provider HTTP client factory which captures only the declared endpoint bodies."""

    def __init__(
        self,
        *,
        claude_proxy: str | None,
        claude_proxy_ca: Path | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._claude_proxy = claude_proxy
        self._claude_proxy_ca = claude_proxy_ca
        self._transport = transport
        self.responses: dict[str, RawUpstreamResponse] = {}

    def __call__(self, capture_key: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient:
        async def capture(response: httpx.Response) -> None:
            if str(response.request.url) not in response_urls:
                return
            content = await response.aread()
            status_code = response.status_code
            content_type = response.headers.get("Content-Type")
            if str(response.request.url).endswith("/api-call"):
                try:
                    envelope = json.loads(content)
                    if isinstance(envelope, dict) and isinstance(envelope.get("status_code"), int):
                        status_code = envelope["status_code"]
                        inner_body = envelope.get("body", "")
                        if isinstance(inner_body, str):
                            content = inner_body.encode()
                        inner_headers = envelope.get("header")
                        if isinstance(inner_headers, dict):
                            raw_content_type = inner_headers.get("Content-Type") or inner_headers.get("content-type")
                            if isinstance(raw_content_type, list) and raw_content_type:
                                raw_content_type = raw_content_type[0]
                            if isinstance(raw_content_type, str):
                                content_type = raw_content_type
                except TypeError, ValueError, json.JSONDecodeError:
                    pass
            truncated = len(content) > _MAX_CAPTURE_BYTES
            captured = content[:_MAX_CAPTURE_BYTES]
            try:
                body: object = json.loads(captured)
            except json.JSONDecodeError, UnicodeDecodeError:
                body = captured.decode("utf-8", errors="replace")
            self.responses[capture_key] = RawUpstreamResponse(
                status_code=status_code,
                content_type=content_type,
                body=body,
                body_base64=base64.b64encode(captured).decode(),
                body_sha256=hashlib.sha256(content).hexdigest(),
                body_size_bytes=len(content),
                truncated=truncated,
            )

        kwargs: dict[str, object] = {
            "timeout": timeout,
            "event_hooks": {"response": [capture]},
            # Do not inherit a pod-wide HTTPS_PROXY. The legacy Claude
            # credential-substitution proxy is used only for the direct-token
            # fallback; CLIProxyAPI management calls must go directly to the
            # in-cluster service.
            "trust_env": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        # Route only direct Claude provider requests through the legacy
        # credential-substitution proxy. Management calls have a local
        # `/api-call` response URL even when their capture key names a Claude
        # endpoint, so the key alone cannot select the proxy.
        is_direct_claude_endpoint = capture_key == "claude_profile_direct" or (
            capture_key in {"claude", "claude_prepaid_balance"}
            and any(url.startswith("https://api.anthropic.com/") for url in response_urls)
        )
        if is_direct_claude_endpoint and self._claude_proxy:
            kwargs["proxy"] = self._claude_proxy
            if self._claude_proxy_ca is not None:
                kwargs["verify"] = str(self._claude_proxy_ca)
        return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]


class QuotaAPIService:
    """In-memory, coalesced quota collector with no disk-backed cache."""

    def __init__(
        self,
        config: Config,
        *,
        cache_ttl: timedelta = timedelta(seconds=120),
        claude_proxy: str | None = None,
        claude_proxy_ca: Path | None = None,
        cli_proxy_api_key: str | None = None,
    ) -> None:
        self._config = config
        self._cache_ttl = cache_ttl
        self._claude_proxy = claude_proxy
        self._claude_proxy_ca = claude_proxy_ca
        self._cli_proxy_api_key = cli_proxy_api_key
        self._snapshot: QuotaSnapshot | None = None
        self._lock = asyncio.Lock()

    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
        if not force_refresh and self._is_fresh(self._snapshot):
            assert self._snapshot is not None
            return self._snapshot
        async with self._lock:
            if not force_refresh and self._is_fresh(self._snapshot):
                assert self._snapshot is not None
                return self._snapshot
            factory = self._client_factory()
            providers = _instantiate(self._config, client_factory=factory, cli_proxy_api_key=self._cli_proxy_api_key)
            outputs = await asyncio.gather(*(provider.fetch() for provider in providers))
            prior = {quota.provider: quota for quota in self._snapshot.quotas.providers} if self._snapshot else {}
            quotas = AllQuotas(
                providers=[
                    _assemble(provider.name, output, prior.get(provider.name))
                    for provider, output in zip(providers, outputs, strict=True)
                ],
                fetched_at=datetime.now(UTC),
            )
            self._snapshot = QuotaSnapshot(quotas=quotas, raw_responses=factory.responses)
            return self._snapshot

    async def fetch_history(self) -> HistorySnapshot:
        """Read every enabled provider's history endpoints once.

        Deliberately outside the quota cache and its lock: history collection
        runs on its own schedule and must not make a `/v1/quotas` caller wait.
        """

        factory = self._client_factory()
        providers = _instantiate(self._config, client_factory=factory, cli_proxy_api_key=self._cli_proxy_api_key)
        batches = await asyncio.gather(
            *(provider.fetch_history() for provider in providers if isinstance(provider, SupportsHistory))
        )
        return HistorySnapshot(
            observations=[observation for batch in batches for observation in batch],
            fetched_at=datetime.now(UTC),
            raw_responses=factory.responses,
        )

    def _client_factory(self) -> _CapturingClientFactory:
        return _CapturingClientFactory(
            claude_proxy=(self._claude_proxy if not self._config.cli_proxy_api.url else None),
            claude_proxy_ca=(self._claude_proxy_ca if not self._config.cli_proxy_api.url else None),
        )

    def _is_fresh(self, snapshot: QuotaSnapshot | None) -> bool:
        return snapshot is not None and datetime.now(UTC) - snapshot.quotas.fetched_at < self._cache_ttl


def _fetcher(request: Request) -> SnapshotFetcher:
    return cast(SnapshotFetcher, request.app.state.fetcher)


def _require_api_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)], request: Request
) -> None:
    expected = cast(str, request.app.state.bearer_token)
    if (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and hmac.compare_digest(credentials.credentials, expected)
    ):
        return
    oauth_username = request.app.state.oauth_username
    if isinstance(oauth_username, str) and request.scope.get("session", {}).get("aiquota_user") == oauth_username:
        return
    raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class BrowserOAuth:
    issuer: str
    client_id: str
    client_secret: str
    session_secret: str
    public_base_url: str
    username: str

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    @property
    def callback_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"


def create_app(
    *,
    bearer_token: str,
    fetcher: SnapshotFetcher,
    collector: BackgroundCollector | None = None,
    history_collector: HistoryCollector | None = None,
    metrics: CollectorMetrics | None = None,
    frontend_dir: Path | None = None,
    browser_oauth: BrowserOAuth | None = None,
    prepare_storage: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    app_metrics = metrics or (collector.metrics if collector else CollectorMetrics())
    feed = fetcher if isinstance(fetcher, SnapshotFeed) else SnapshotFeed(fetcher)
    if collector is not None:
        collector.bind_fetcher(feed)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A failure ends the process, so the Pod restarts until the schema applies.
        if prepare_storage is not None:
            await prepare_storage()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(feed.run_time_transitions, name="aiquota-view-transitions")
            if collector is not None:
                tasks.start_soon(collector.run, name="aiquota-clickhouse-collector")
            if history_collector is not None:
                tasks.start_soon(history_collector.run, name="aiquota-history-collector")
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()

    app = FastAPI(title="aiquota API", version="1", lifespan=lifespan)
    app.state.bearer_token = bearer_token
    app.state.fetcher = feed
    app.state.snapshot_feed = feed
    app.state.oauth_username = browser_oauth.username if browser_oauth else None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if not all(runner is None or runner.is_running for runner in (collector, history_collector)):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        if collector is None or not collector.requires_persistence_for_readiness:
            return JSONResponse({"status": "ready"})
        if collector.has_persisted:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "collecting"}, status_code=503)

    @app.get("/metrics")
    async def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(app_metrics.registry).decode(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/quotas", dependencies=[Depends(_require_api_auth)])
    async def quotas(service: Annotated[SnapshotFetcher, Depends(_fetcher)], response: Response) -> AllQuotasView:
        snapshot = await service.fetch()
        response.headers.update(_CACHE_CONTROL)
        return to_view(snapshot.quotas)

    @app.get("/v1/events", dependencies=[Depends(_require_api_auth)])
    async def events(request: Request) -> StreamingResponse:
        await feed.ensure_current()

        async def event_stream() -> AsyncIterator[bytes]:
            subscription = await feed.subscribe_current()
            revision = subscription.revision
            try:
                yield _view_event(subscription.snapshot)
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        revision, snapshot = await asyncio.wait_for(
                            feed.wait_after(revision), timeout=_SSE_HEARTBEAT_SECONDS
                        )
                    except TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield b": heartbeat\n\n"
                    else:
                        yield _view_event(snapshot)
            finally:
                await feed.close_subscription(subscription.token)

        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers={**_CACHE_CONTROL, "X-Accel-Buffering": "no"}
        )

    @app.get("/v1/providers/{provider}/raw", dependencies=[Depends(_require_api_auth)])
    async def provider_raw(provider: str, service: Annotated[SnapshotFetcher, Depends(_fetcher)]) -> JSONResponse:
        snapshot = await service.fetch()
        if provider not in {quota.provider for quota in snapshot.quotas.providers}:
            raise HTTPException(status_code=404, detail="unknown provider", headers=_CACHE_CONTROL)
        return raw_response(provider, provider, snapshot)

    @app.get("/v1/providers/{provider}/prepaid-balance/raw", dependencies=[Depends(_require_api_auth)])
    async def provider_prepaid_balance_raw(
        provider: str, service: Annotated[SnapshotFetcher, Depends(_fetcher)]
    ) -> JSONResponse:
        snapshot = await service.fetch()
        if provider not in {quota.provider for quota in snapshot.quotas.providers}:
            raise HTTPException(status_code=404, detail="unknown provider", headers=_CACHE_CONTROL)
        if provider != "claude":
            raise HTTPException(status_code=404, detail="unknown provider endpoint", headers=_CACHE_CONTROL)
        return raw_response(provider, "claude_prepaid_balance", snapshot)

    def raw_response(provider: str, capture_key: str, snapshot: QuotaSnapshot) -> JSONResponse:
        raw = snapshot.raw_responses.get(capture_key)
        if raw is None:
            raise HTTPException(status_code=503, detail="no upstream response available", headers=_CACHE_CONTROL)
        return JSONResponse(
            {
                "provider": provider,
                "fetched_at": snapshot.quotas.fetched_at.isoformat(),
                "upstream": raw.model_dump(mode="json"),
            },
            headers=_CACHE_CONTROL,
        )

    if browser_oauth is not None:
        oauth = OAuth()
        oauth.register(
            name="authentik",
            client_id=browser_oauth.client_id,
            client_secret=browser_oauth.client_secret,
            server_metadata_url=browser_oauth.metadata_url,
            client_kwargs={"scope": "openid email profile"},
        )

        @app.get("/auth/login")
        async def oauth_login(request: Request) -> RedirectResponse:
            client = oauth.create_client("authentik")
            return cast(
                RedirectResponse,
                await client.authorize_redirect(request, browser_oauth.callback_url, nonce=secrets.token_urlsafe(32)),
            )

        @app.get("/auth/callback")
        async def oauth_callback(request: Request) -> RedirectResponse:
            client = oauth.create_client("authentik")
            try:
                token = await client.authorize_access_token(request)
            except OAuthError as error:
                logger.info("aiquota OAuth callback failed: %s", error.error)
                raise HTTPException(status_code=401, detail="OAuth login failed") from error
            userinfo = token.get("userinfo") or {}
            if (
                userinfo.get("iss") != browser_oauth.issuer
                or userinfo.get("preferred_username") != browser_oauth.username
            ):
                raise HTTPException(status_code=403, detail="OAuth identity is not authorized")
            request.session["aiquota_user"] = browser_oauth.username
            return RedirectResponse(url="/", status_code=303)

        @app.get("/", response_model=None)
        async def frontend_entry(request: Request) -> RedirectResponse | object:
            if request.session.get("aiquota_user") != browser_oauth.username:
                return RedirectResponse(url="/auth/login", status_code=303)
            return FileResponse(frontend_dir / "index.html") if frontend_dir else {"status": "frontend unavailable"}

        app.add_middleware(
            SessionMiddleware,
            secret_key=browser_oauth.session_secret,
            https_only=browser_oauth.public_base_url.startswith("https://"),
            same_site="lax",
            max_age=3600,
        )

    if frontend_dir is None:
        # Only the server binaries package the SPA. The API library is also imported without
        # it — by its own tests, and by the OpenAPI exporter the frontend's types come from,
        # which cannot depend on the bundle it generates types for.
        index = find_path(_FRONTEND_INDEX)
        if index is None:
            logger.warning("aiquota frontend bundle is not present in runfiles")
        else:
            frontend_dir = index.parent
    if frontend_dir is not None:
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


def main() -> None:
    settings = Settings()
    service = QuotaAPIService(
        load_config(settings.config_path),
        cache_ttl=settings.cache_ttl,
        claude_proxy=settings.claude_proxy,
        claude_proxy_ca=settings.claude_proxy_ca,
        cli_proxy_api_key=settings.cli_proxy_api_key.get_secret_value() if settings.cli_proxy_api_key else None,
    )
    metrics = CollectorMetrics()
    feed = SnapshotFeed(service)
    sink: ClickHouseSnapshotSink | None = None
    history_collector: HistoryCollector | None = None
    if settings.clickhouse_url:
        assert settings.clickhouse_password is not None
        sink = ClickHouseSnapshotSink(
            url=settings.clickhouse_url,
            username=settings.clickhouse_username,
            password=settings.clickhouse_password.get_secret_value(),
            database=settings.clickhouse_database,
            raw_table=settings.clickhouse_raw_table,
            windows_table=settings.clickhouse_windows_table,
        )
        history_collector = HistoryCollector(
            service, sink, interval=timedelta(seconds=settings.history_interval_seconds), metrics=metrics
        )
    collector = BackgroundCollector(
        feed, sink, interval=timedelta(seconds=settings.poll_interval_seconds), metrics=metrics
    )
    uvicorn.run(
        create_app(
            bearer_token=settings.api_bearer_token,
            fetcher=feed,
            collector=collector,
            history_collector=history_collector,
            metrics=metrics,
            prepare_storage=None if sink is None else sink.apply_schema,
            browser_oauth=BrowserOAuth(
                issuer=settings.oauth_issuer,
                client_id=settings.oauth_client_id,
                client_secret=settings.oauth_client_secret.get_secret_value(),
                session_secret=settings.oauth_session_secret.get_secret_value(),
                public_base_url=settings.public_base_url,
                username=settings.oauth_username,
            ),
        ),
        host="0.0.0.0",
        port=8080,
    )


if __name__ == "__main__":
    main()
