"""Bearer-authenticated HTTP API for the aiquota provider adapters.

The API deliberately keeps provider responses in process memory only.  It is
safe to use beside a credential-refresh owner (for example CLIProxyAPI): its
provider settings disable refreshes, so this process never mutates credential
files or persists quota snapshots.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, cast

import httpx
import uvicorn
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.sessions import SessionMiddleware

from aiquota.cache import _assemble, _instantiate
from aiquota.clickhouse import ClickHouseSnapshotSink
from aiquota.config import Config, load as load_config
from aiquota.models import AllQuotas, FetchSuccess, HistoryObservation
from aiquota.providers.base import SupportsHistory
from util.bazel.runfiles import get_required_path

_CACHE_CONTROL = {"Cache-Control": "no-store"}
_MAX_CAPTURE_BYTES = 1024 * 1024
_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

_FRONTEND_INDEX = "_main/aiquota/frontend/dist/index.html"


class RawUpstreamResponse(BaseModel):
    """The safely exposable parts of one quota-endpoint response.

    Request and response headers are deliberately excluded: they can contain
    credentials, cookies, account identifiers, or proxy implementation data.
    """

    status_code: int
    content_type: str | None = None
    body: object | None = None
    body_base64: str | None = None
    body_sha256: str | None = None
    body_size_bytes: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class QuotaSnapshot:
    quotas: AllQuotas
    raw_responses: dict[str, RawUpstreamResponse]


@dataclass(frozen=True)
class HistorySnapshot:
    observations: list[HistoryObservation]
    fetched_at: datetime
    raw_responses: dict[str, RawUpstreamResponse]


class SnapshotFetcher(Protocol):
    async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot: ...


class SnapshotSink(Protocol):
    async def write(self, snapshot: QuotaSnapshot) -> int: ...


class HistoryFetcher(Protocol):
    async def fetch_history(self) -> HistorySnapshot: ...


class HistorySink(Protocol):
    async def write_history(self, snapshot: HistorySnapshot) -> int: ...


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
    """Continuously refresh providers and append each snapshot to a sink."""

    def __init__(
        self, fetcher: SnapshotFetcher, sink: SnapshotSink, *, interval: timedelta, metrics: CollectorMetrics
    ) -> None:
        self._fetcher = fetcher
        self._sink = sink
        self._interval = interval
        self.metrics = metrics
        self.has_persisted = False

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval.total_seconds())

    async def poll_once(self) -> None:
        try:
            snapshot = await self._fetcher.fetch(force_refresh=True)
            for provider in snapshot.quotas.providers:
                self.metrics.provider_success.labels(provider=provider.provider).set(
                    1 if isinstance(provider.last_output.result, FetchSuccess) else 0
                )
            rows = await self._sink.write(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.polls.labels(result="error").inc()
            self.metrics.clickhouse_writes.labels(result="error").inc()
            logger.exception("aiquota background collection failed")
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

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval.total_seconds())

    async def poll_once(self) -> None:
        try:
            rows = await self._sink.write_history(await self._fetcher.fetch_history())
        except asyncio.CancelledError:
            raise
        except Exception:
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
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            truncated = len(content) > _MAX_CAPTURE_BYTES
            captured = content[:_MAX_CAPTURE_BYTES]
            try:
                body: object = json.loads(captured)
            except (json.JSONDecodeError, UnicodeDecodeError):
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
        # Only Claude's own quota endpoint sits behind the credential-substitution
        # proxy; a per-endpoint capture key never matches this provider name.
        if capture_key == "claude" and self._claude_proxy:
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


class Settings(BaseSettings):
    """Runtime configuration sourced from the aiquota Deployment environment."""

    model_config = SettingsConfigDict(env_prefix="AIQUOTA_", extra="ignore")

    api_bearer_token: str
    config_path: Path = Field(default=Path("/etc/aiquota/config.toml"), validation_alias="AIQUOTA_CONFIG")
    cache_ttl_seconds: int = Field(default=120, ge=0)
    claude_proxy: str | None = None
    claude_proxy_ca: Path | None = None
    cli_proxy_api_key: SecretStr | None = Field(default=None, validation_alias="AIQUOTA_CLIPROXY_API_KEY")
    clickhouse_url: str | None = None
    clickhouse_username: str = "aiquota_ingest"
    clickhouse_password: SecretStr | None = None
    clickhouse_database: str = "aiquota"
    clickhouse_raw_table: str = "raw_http_observations"
    clickhouse_windows_table: str = "aiquota_windows"
    poll_interval_seconds: int = Field(default=300, gt=0)
    history_interval_seconds: int = Field(default=3600, gt=0)
    public_base_url: str = "https://aiquota.allegedly.works"
    oauth_issuer: str
    oauth_client_id: str
    oauth_client_secret: SecretStr
    oauth_session_secret: SecretStr
    oauth_username: str = "agentydragon"

    @model_validator(mode="after")
    def validate_clickhouse(self) -> "Settings":
        if self.clickhouse_url and self.clickhouse_password is None:
            raise ValueError("AIQUOTA_CLICKHOUSE_PASSWORD is required when AIQUOTA_CLICKHOUSE_URL is set")
        return self

    @property
    def cache_ttl(self) -> timedelta:
        return timedelta(seconds=self.cache_ttl_seconds)


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


def _with_remaining_percent(value: object) -> object:
    """Add derived remaining percentage without changing aiquota's shared model."""

    if not isinstance(value, dict):
        return value
    result = dict(value)
    windows = result.get("windows")
    if isinstance(windows, list):
        result["windows"] = [
            {**window, "remaining_percent": max(0.0, 100.0 - float(window["used_percent"]))}
            if isinstance(window, dict) and isinstance(window.get("used_percent"), int | float)
            else window
            for window in windows
        ]
    return result


def _normalized_payload(quotas: AllQuotas) -> dict[str, object]:
    payload = quotas.model_dump(mode="json")
    providers = cast(list[dict[str, object]], payload["providers"])
    for provider in providers:
        for key in ("last_output", "last_success"):
            fetch = provider.get(key)
            if isinstance(fetch, dict):
                fetch["result"] = _with_remaining_percent(fetch.get("result"))
    return payload


def create_app(
    *,
    bearer_token: str,
    fetcher: SnapshotFetcher,
    collector: BackgroundCollector | None = None,
    history_collector: HistoryCollector | None = None,
    metrics: CollectorMetrics | None = None,
    frontend_dir: Path | None = None,
    browser_oauth: BrowserOAuth | None = None,
) -> FastAPI:
    app_metrics = metrics or (collector.metrics if collector else CollectorMetrics())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runners = {"aiquota-clickhouse-collector": collector, "aiquota-history-collector": history_collector}
        tasks = [asyncio.create_task(runner.run(), name=name) for name, runner in runners.items() if runner is not None]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="aiquota API", version="1", lifespan=lifespan)
    app.state.bearer_token = bearer_token
    app.state.fetcher = fetcher
    app.state.oauth_username = browser_oauth.username if browser_oauth else None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = collector is None or collector.has_persisted
        return JSONResponse({"status": "ready" if ready else "collecting"}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(app_metrics.registry).decode(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/quotas", dependencies=[Depends(_require_api_auth)])
    async def quotas(service: Annotated[SnapshotFetcher, Depends(_fetcher)]) -> JSONResponse:
        snapshot = await service.fetch()
        return JSONResponse(_normalized_payload(snapshot.quotas), headers=_CACHE_CONTROL)

    @app.get("/v1/providers/{provider}/raw", dependencies=[Depends(_require_api_auth)])
    async def provider_raw(provider: str, service: Annotated[SnapshotFetcher, Depends(_fetcher)]) -> JSONResponse:
        snapshot = await service.fetch()
        if provider not in {quota.provider for quota in snapshot.quotas.providers}:
            raise HTTPException(status_code=404, detail="unknown provider", headers=_CACHE_CONTROL)
        raw = snapshot.raw_responses.get(provider)
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
        try:
            frontend_dir = get_required_path(_FRONTEND_INDEX).parent
        except FileNotFoundError:
            # Unit tests exercise the API library without packaging its SPA.
            logger.warning("aiquota frontend bundle is not present in runfiles")
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
    collector: BackgroundCollector | None = None
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
        collector = BackgroundCollector(
            service, sink, interval=timedelta(seconds=settings.poll_interval_seconds), metrics=metrics
        )
        history_collector = HistoryCollector(
            service, sink, interval=timedelta(seconds=settings.history_interval_seconds), metrics=metrics
        )
    uvicorn.run(
        create_app(
            bearer_token=settings.api_bearer_token,
            fetcher=service,
            collector=collector,
            history_collector=history_collector,
            metrics=metrics,
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
