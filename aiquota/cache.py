import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platformdirs import user_cache_dir

from aiquota.config import Config
from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, SuccessfulProviderFetch
from aiquota.providers.base import Provider
from aiquota.providers.claude import ClaudeProvider
from aiquota.providers.codex import CodexProvider
from aiquota.providers.zai import ZaiProvider

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

    async def fetch_all(self, providers: list[Provider]) -> AllQuotas:
        cached = self.read()
        if cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return cached
        prior = {pq.provider: pq for pq in cached.providers} if cached else {}
        outputs = await asyncio.gather(*(p.fetch() for p in providers))
        results = [_assemble(p.name, out, prior.get(p.name)) for p, out in zip(providers, outputs, strict=True)]
        fresh = AllQuotas(providers=results, fetched_at=datetime.now(UTC))
        self.write(fresh)
        return fresh


class QuotaService:
    def __init__(self, config: Config, cache: QuotaCache | None = None) -> None:
        self.providers = _instantiate(config)
        self.cache = cache or QuotaCache()

    async def fetch_all(self) -> AllQuotas:
        return await self.cache.fetch_all(self.providers)


def _instantiate(config: Config) -> list[Provider]:
    """Build the enabled provider instances in display order."""
    candidates: list[tuple[Provider, bool]] = [
        (ClaudeProvider(config.claude), config.claude.enabled),
        (CodexProvider(config.codex), config.codex.enabled),
        (ZaiProvider(config.zai), config.zai.enabled),
    ]
    return [p for p, enabled in candidates if enabled]


def _assemble(name: str, output: ProviderFetch, prior: ProviderQuota | None) -> ProviderQuota:
    """Wrap a provider fetch in a `ProviderQuota`, carrying the last-known-good
    snapshot forward when the latest call did not produce usable windows."""
    success: SuccessfulProviderFetch | None = None
    if isinstance(output.result, FetchSuccess) and (output.result.short_window or output.result.long_window):
        success = SuccessfulProviderFetch(fetched_at=output.fetched_at, result=output.result)
    return ProviderQuota(
        provider=name, last_output=output, last_success=success or (prior.last_success if prior else None)
    )
