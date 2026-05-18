from datetime import UTC, datetime, timedelta
from pathlib import Path

from platformdirs import user_cache_dir

from aiquota.config import ConfigFile
from aiquota.models import AllQuotas, ProviderFetch, ProviderQuota
from aiquota.providers import claude, codex, zai

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

    def fetch_all(self, config: ConfigFile) -> AllQuotas:
        cached = self.read()
        if cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return cached
        prior = {pq.provider: pq for pq in cached.providers} if cached else {}
        providers = [_assemble(name, fetch, prior.get(name)) for name, fetch in _fetch_providers(config)]
        fresh = AllQuotas(providers=providers, fetched_at=datetime.now(UTC))
        self.write(fresh)
        return fresh


class QuotaService:
    def __init__(self, config: ConfigFile, cache: QuotaCache | None = None) -> None:
        self.config = config
        self.cache = cache or QuotaCache()

    def fetch_all(self) -> AllQuotas:
        return self.cache.fetch_all(self.config)


def _assemble(name: str, output: ProviderFetch, prior: ProviderQuota | None) -> ProviderQuota:
    """Wrap a provider fetch in a `ProviderQuota`, carrying the last-known-good
    snapshot forward when the latest call did not produce usable windows."""
    success = output if output.error is None and (output.short_window or output.long_window) else None
    return ProviderQuota(
        provider=name, last_output=output, last_success=success or (prior.last_success if prior else None)
    )


def _fetch_providers(config: ConfigFile) -> list[tuple[str, ProviderFetch]]:
    results: list[tuple[str, ProviderFetch]] = []
    for name, fetch_fn in [("claude", claude.fetch), ("codex", codex.fetch)]:
        settings = config.providers.get(name)
        if settings is not None and not settings.enabled:
            continue
        results.append((name, fetch_fn()))

    zai_settings = config.providers.get("zai")
    if zai_settings is None or zai_settings.enabled:
        results.append(("zai", zai.fetch(api_key_path=zai_settings.api_key_path if zai_settings else None)))

    return results
