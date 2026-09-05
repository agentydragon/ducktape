import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platformdirs import user_cache_dir

from aiquota.config import Config
from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, SuccessfulProviderFetch
from aiquota.providers.base import Provider
from aiquota.providers.claude import ClaudeProvider
from aiquota.providers.cli_proxy_api import CLIProxyAPIManagementClient
from aiquota.providers.client import ProviderClientFactory, provider_client
from aiquota.providers.codex import CodexProvider
from aiquota.providers.zai import ZaiProvider
from aiquota.remote import QuotaAPIClient

CACHE_TTL = timedelta(seconds=120)


class QuotaCache:
    def __init__(self, path: Path | None = None, ttl: timedelta = CACHE_TTL) -> None:
        self.path = path or Path(user_cache_dir("aiquota")) / "quotas.json"
        self.ttl = ttl

    def read(self) -> AllQuotas | None:
        try:
            return AllQuotas.model_validate_json(self.path.read_text())
        except (OSError, ValueError):
            return None

    def write(self, quotas: AllQuotas) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(quotas.model_dump_json())
        except OSError:
            pass

    async def fetch_all(self, providers: list[Provider], force_refresh: bool = False) -> AllQuotas:
        cached = self.read()
        if not force_refresh and cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return cached
        prior = {pq.provider: pq for pq in cached.providers} if cached else {}
        outputs = await asyncio.gather(*(p.fetch() for p in providers))
        results = [_assemble(p.name, out, prior.get(p.name)) for p, out in zip(providers, outputs, strict=True)]
        fresh = AllQuotas(providers=results, fetched_at=datetime.now(UTC))
        self.write(fresh)
        return fresh

    async def fetch_remote(self, fetcher: Callable[[], Awaitable[AllQuotas]], force_refresh: bool = False) -> AllQuotas:
        cached = self.read()
        if not force_refresh and cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return cached
        try:
            fresh = await fetcher()
        except Exception:
            if cached is None:
                raise
            return cached
        self.write(fresh)
        return fresh


class QuotaService:
    def __init__(self, config: Config, cache: QuotaCache | None = None, debug: bool = False) -> None:
        self.remote_client = (
            QuotaAPIClient(
                config.remote_api.url,
                config.remote_api.bearer_token.get_secret_value() if config.remote_api.bearer_token else None,
            )
            if config.remote_api.url
            else None
        )
        self.providers = [] if self.remote_client else _instantiate(config, client_factory=provider_client(debug))
        self.cache = cache or QuotaCache()
        self.debug = debug

    async def fetch_all(self) -> AllQuotas:
        if self.remote_client:
            return await self.cache.fetch_remote(self.remote_client.fetch, force_refresh=self.debug)
        return await self.cache.fetch_all(self.providers, force_refresh=self.debug)


def _instantiate(
    config: Config, client_factory: ProviderClientFactory, cli_proxy_api_key: str | None = None
) -> list[Provider]:
    """Build the enabled provider instances in display order."""
    management_client = (
        CLIProxyAPIManagementClient(url=config.cli_proxy_api.url, key=cli_proxy_api_key, client_factory=client_factory)
        if config.cli_proxy_api.url
        else None
    )
    candidates: list[tuple[Provider, bool]] = [
        (ClaudeProvider(config.claude, client_factory, management_client), config.claude.enabled),
        (CodexProvider(config.codex, client_factory, management_client), config.codex.enabled),
        (ZaiProvider(config.zai, client_factory), config.zai.enabled),
    ]
    return [p for p, enabled in candidates if enabled]


def _assemble(name: str, output: ProviderFetch, prior: ProviderQuota | None) -> ProviderQuota:
    """Wrap a provider fetch, retaining the last substantive quota snapshot."""
    success: SuccessfulProviderFetch | None = None
    if isinstance(output.result, FetchSuccess) and (
        output.result.windows or output.result.available_reset_credits is not None
    ):
        success = SuccessfulProviderFetch(fetched_at=output.fetched_at, result=output.result)
    return ProviderQuota(
        provider=name, last_output=output, last_success=success or (prior.last_success if prior else None)
    )
