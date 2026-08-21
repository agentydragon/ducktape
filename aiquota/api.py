"""Bearer-authenticated HTTP API for the aiquota provider adapters.

The API deliberately keeps provider responses in process memory only.  It is
safe to use beside a credential-refresh owner (for example CLIProxyAPI): its
provider settings disable refreshes, so this process never mutates credential
files or persists quota snapshots.
"""

import asyncio
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, cast

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aiquota.cache import _assemble, _instantiate
from aiquota.config import Config, load as load_config
from aiquota.models import AllQuotas
from aiquota.providers.client import ProviderClientFactory

_CACHE_CONTROL = {"Cache-Control": "no-store"}
_MAX_CAPTURE_BYTES = 1024 * 1024
_bearer = HTTPBearer(auto_error=False)


class RawUpstreamResponse(BaseModel):
    """The safely exposable parts of one quota-endpoint response.

    Request and response headers are deliberately excluded: they can contain
    credentials, cookies, account identifiers, or proxy implementation data.
    """

    status_code: int
    content_type: str | None = None
    body: object | None = None
    truncated: bool = False


@dataclass(frozen=True)
class QuotaSnapshot:
    quotas: AllQuotas
    raw_responses: dict[str, RawUpstreamResponse]


class SnapshotFetcher(Protocol):
    async def fetch(self) -> QuotaSnapshot: ...


class _CapturingClientFactory:
    """Provider HTTP client factory which captures only quota endpoint bodies."""

    def __init__(self, *, claude_proxy: str | None, claude_proxy_ca: Path | None) -> None:
        self._claude_proxy = claude_proxy
        self._claude_proxy_ca = claude_proxy_ca
        self.responses: dict[str, RawUpstreamResponse] = {}

    def __call__(self, provider: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient:
        async def capture(response: httpx.Response) -> None:
            if str(response.request.url) not in response_urls:
                return
            content = await response.aread()
            status_code = response.status_code
            content_type = response.headers.get("Content-Type")
            if provider == "codex" and str(response.request.url).endswith("/api-call"):
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
            self.responses[provider] = RawUpstreamResponse(
                status_code=status_code, content_type=content_type, body=body, truncated=truncated
            )

        kwargs: dict[str, object] = {
            "timeout": timeout,
            "event_hooks": {"response": [capture]},
            # Do not inherit a pod-wide HTTPS_PROXY: only Claude needs the
            # credential-substitution proxy.  Codex goes directly to its usage
            # endpoint using the read-only OAuth file shared with CLIProxyAPI.
            "trust_env": False,
        }
        if provider == "claude" and self._claude_proxy:
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

    async def fetch(self) -> QuotaSnapshot:
        if self._is_fresh(self._snapshot):
            assert self._snapshot is not None
            return self._snapshot
        async with self._lock:
            if self._is_fresh(self._snapshot):
                assert self._snapshot is not None
                return self._snapshot
            factory: ProviderClientFactory = _CapturingClientFactory(
                claude_proxy=self._claude_proxy, claude_proxy_ca=self._claude_proxy_ca
            )
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
            self._snapshot = QuotaSnapshot(
                quotas=quotas, raw_responses=cast(_CapturingClientFactory, factory).responses
            )
            return self._snapshot

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

    @property
    def cache_ttl(self) -> timedelta:
        return timedelta(seconds=self.cache_ttl_seconds)


def _fetcher(request: Request) -> SnapshotFetcher:
    return cast(SnapshotFetcher, request.app.state.fetcher)


def _require_bearer(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)], request: Request
) -> None:
    expected = cast(str, request.app.state.bearer_token)
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})


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


def create_app(*, bearer_token: str, fetcher: SnapshotFetcher) -> FastAPI:
    app = FastAPI(title="aiquota API", version="1")
    app.state.bearer_token = bearer_token
    app.state.fetcher = fetcher

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/quotas", dependencies=[Depends(_require_bearer)])
    async def quotas(service: Annotated[SnapshotFetcher, Depends(_fetcher)]) -> JSONResponse:
        snapshot = await service.fetch()
        return JSONResponse(_normalized_payload(snapshot.quotas), headers=_CACHE_CONTROL)

    @app.get("/v1/providers/{provider}/raw", dependencies=[Depends(_require_bearer)])
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
    uvicorn.run(create_app(bearer_token=settings.api_bearer_token, fetcher=service), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
