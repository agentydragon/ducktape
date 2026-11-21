import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from platformdirs import user_cache_dir

from aiquota.config import Config
from aiquota.models import (
    AllQuotas,
    FetchError,
    FetchSuccess,
    ProviderFetch,
    ProviderQuota,
    SuccessfulProviderFetch,
    is_substantive,
)
from aiquota.providers.anthropic_platform import AnthropicPlatformProvider
from aiquota.providers.base import Provider
from aiquota.providers.claude import (
    _OAUTH_HEADERS,
    PROFILE_URL as CLAUDE_PROFILE_URL,
    ClaudeProvider,
    _ClaudeProfileResponse,
)
from aiquota.providers.cli_proxy_api import CLIProxyAPIManagementClient
from aiquota.providers.client import ProviderClientFactory, provider_client
from aiquota.providers.codex import CodexProvider
from aiquota.providers.openai_platform import OpenAIPlatformProvider
from aiquota.providers.zai import ZaiProvider
from aiquota.remote import QuotaAPIClient
from aiquota.render.view_model import AllQuotasView, refresh_view, to_view

CACHE_TTL = timedelta(seconds=120)


class QuotaCache:
    def __init__(self, path: Path | None = None, ttl: timedelta = CACHE_TTL) -> None:
        self.path = path or Path(user_cache_dir("aiquota")) / "quotas.json"
        self.ttl = ttl

    def read(self) -> AllQuotas | None:
        try:
            return AllQuotas.model_validate_json(self.path.read_text())
        except OSError, ValueError:
            return None

    def write(self, quotas: AllQuotas | AllQuotasView) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(quotas.model_dump_json())
        except OSError:
            pass

    async def fetch_all(self, providers: list[Provider], force_refresh: bool = False) -> AllQuotas:
        cached = self.read()
        if not force_refresh and cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return cached
        prior = {(pq.provider, pq.account_id): pq for pq in cached.providers} if cached else {}
        outputs = await asyncio.gather(*(p.fetch() for p in providers))
        results = [
            _assemble(
                p.name,
                out,
                prior.get((p.name, p.account_id)),
                account_id=p.account_id,
                account_label=p.account_label,
                plan_type=p.plan_type,
                account_tier=p.account_tier,
            )
            for p, out in zip(providers, outputs, strict=True)
        ]
        fresh = AllQuotas(providers=results, fetched_at=datetime.now(UTC))
        self.write(fresh)
        return fresh

    async def fetch_remote_view(
        self, fetcher: Callable[[], Awaitable[AllQuotasView]], force_refresh: bool = False
    ) -> AllQuotasView:
        cached: AllQuotasView | None
        try:
            cached = AllQuotasView.model_validate_json(self.path.read_text())
        except OSError, ValueError:
            raw = self.read()
            cached = to_view(raw) if raw is not None else None
        if not force_refresh and cached is not None and datetime.now(UTC) - cached.fetched_at < self.ttl:
            return refresh_view(cached)
        try:
            fresh = await fetcher()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if cached is None:
                raise
            return refresh_view(cached, connection_error=str(exc))
        self.write(fresh)
        return refresh_view(fresh)


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
            view = await self.cache.fetch_remote_view(self.remote_client.fetch, force_refresh=self.debug)
            return AllQuotas.model_validate(view, from_attributes=True)
        return await self.cache.fetch_all(self.providers, force_refresh=self.debug)

    async def fetch_view(self) -> AllQuotasView:
        if self.remote_client:
            return await self.cache.fetch_remote_view(self.remote_client.fetch, force_refresh=self.debug)
        return to_view(await self.fetch_all())


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
        (OpenAIPlatformProvider(config.openai_platform, client_factory), config.openai_platform.enabled),
        (AnthropicPlatformProvider(config.anthropic_platform, client_factory), config.anthropic_platform.enabled),
        (ZaiProvider(config.zai, client_factory), config.zai.enabled),
    ]
    return [p for p, enabled in candidates if enabled]


async def _instantiate_accounts(
    config: Config, client_factory: ProviderClientFactory, cli_proxy_api_key: str | None
) -> list[Provider]:
    providers = _instantiate(config, client_factory, cli_proxy_api_key)
    if not config.cli_proxy_api.url:
        return providers
    management = CLIProxyAPIManagementClient(config.cli_proxy_api.url, cli_proxy_api_key, client_factory)
    try:
        entries = await management.list_auth_files()
    except Exception as error:
        return [
            _UnavailableProvider(p.name, f"CLIProxyAPI inventory: {type(error).__name__}")
            if p.name in {"claude", "codex"}
            else p
            for p in providers
        ]

    async def account(entry: dict[str, object], provider: Provider) -> Provider:
        index = entry.get("auth_index")
        if not isinstance(index, str) or not index:
            return _UnavailableProvider(provider.name, "credential is missing its auth index")
        bound = replace(management, auth_index=index)
        try:
            if provider.name == "codex":
                claims = entry.get("id_token")
                account_id = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
                plan = claims.get("plan_type") if isinstance(claims, dict) else None
            else:
                body = await bound.fetch_usage(
                    "claude", CLAUDE_PROFILE_URL, _OAUTH_HEADERS, "claude_profile", capture_response=False
                )
                profile = _ClaudeProfileResponse.model_validate_json(body)
                if profile.account is None:
                    raise ValueError("Claude account identity is unavailable")
                account_id = f"{profile.account.uuid}:{profile.organization.uuid}"
                plan = None
            if not isinstance(account_id, str) or not account_id:
                raise ValueError("provider account identity is unavailable")
            bound = replace(bound, account_id=account_id)
            if provider.name == "codex":
                result: Provider = CodexProvider(config.codex, client_factory, bound)
            else:
                result = ClaudeProvider(config.claude, client_factory, bound)
            result.account_id = account_id
            metadata = config.accounts.get(f"{provider.name}:{account_id}")
            email = entry.get("email")
            result.account_label = (
                metadata.label if metadata and metadata.label else email if isinstance(email, str) else None
            )
            result.account_tier = metadata.tier if metadata else None
            result.plan_type = plan if isinstance(plan, str) else None
            return result
        except Exception as error:
            return _UnavailableProvider(provider.name, f"account identity unavailable: {type(error).__name__}")

    expanded: list[Provider] = []
    for provider in providers:
        if provider.name not in {"codex", "claude"}:
            expanded.append(provider)
            continue
        matching = [e for e in entries if str(e.get("provider", "")).lower() == provider.name]
        if not matching:
            expanded.append(_UnavailableProvider(provider.name, "no credential found"))
            continue
        accounts = await asyncio.gather(*(account(e, provider) for e in matching))
        ids = [p.account_id for p in accounts if p.account_id is not None]
        if len(ids) != len(set(ids)):
            expanded.append(_UnavailableProvider(provider.name, "duplicate provider account identity"))
        else:
            expanded.extend(p for p in accounts if p.account_id is not None)
            unknown = [p for p in accounts if p.account_id is None]
            if unknown:
                expanded.append(
                    _UnavailableProvider(provider.name, f"{len(unknown)} credential(s) could not be identified")
                )
    return expanded


class _UnavailableProvider(Provider):
    def __init__(self, name: str, error: str) -> None:
        self.name = name
        self.error = error

    async def fetch(self) -> ProviderFetch:
        return ProviderFetch(fetched_at=datetime.now(UTC), result=FetchError(error=self.error))


def _assemble(
    name: str,
    output: ProviderFetch,
    prior: ProviderQuota | None,
    *,
    account_id: str | None = None,
    account_label: str | None = None,
    plan_type: str | None = None,
    account_tier: str | None = None,
) -> ProviderQuota:
    """Wrap a provider fetch, retaining the last substantive quota snapshot."""
    success: SuccessfulProviderFetch | None = None
    if isinstance(output.result, FetchSuccess) and is_substantive(output.result):
        success = SuccessfulProviderFetch(fetched_at=output.fetched_at, result=output.result)
    return ProviderQuota(
        provider=name,
        account_id=account_id,
        account_label=account_label,
        plan_type=plan_type,
        account_tier=account_tier,
        last_output=output,
        last_success=success or (prior.last_success if prior else None),
    )
